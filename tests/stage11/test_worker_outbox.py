"""Durable attempts, lease fencing and atomic result completion regressions."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from financeclaw.memory_worker.runner import MemoryJobRunner
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.outbox.models import OutboxEvent
from financeclaw.shared.outbox.repository import ModelBudgetExhausted, SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow


@pytest.fixture
def worker_outbox(tmp_path):
    """Use a file database so the runner's independent SQL threads share persisted facts."""
    database = ApplicationDatabase(f"sqlite:///{tmp_path / 'worker.db'}")
    database.initialize_schema()
    repository = SqlAlchemyOutboxRepository(database.session_factory)
    repository.enqueue(
        OutboxEvent(
            event_id="job",
            destination="memory_extract",
            event_type="memory.extract.part",
            aggregate_type="owner",
            aggregate_id="subject",
            tenant_id="tenant",
            subject_id="subject",
        )
    )
    yield database, repository
    database.close()


def claim(repository):
    """Claim the fixture task in its dedicated destination."""
    return repository.claim_pending(destination="memory_extract", limit=1)[0]


def expire(database):
    """Simulate a killed process whose lease is now available for takeover."""
    with database.session_factory.begin() as session:
        session.get(OutboxEventRow, "job").locked_until = datetime.now(UTC) - timedelta(seconds=1)


def test_durable_attempt_budget_survives_takeover(worker_outbox):
    """S17: an unknown model response consumes budget even after process replacement."""
    database, repository = worker_outbox
    first = claim(repository)
    repository.reserve_model_attempt(
        "job", claim_epoch=first.claim_epoch, max_attempts=2, input_tokens=100, output_tokens=50
    )
    expire(database)
    second = claim(SqlAlchemyOutboxRepository(database.session_factory))
    repository.reserve_model_attempt(
        "job", claim_epoch=second.claim_epoch, max_attempts=2, input_tokens=100, output_tokens=50
    )
    with pytest.raises(ModelBudgetExhausted):
        repository.reserve_model_attempt(
            "job", claim_epoch=second.claim_epoch, max_attempts=2, input_tokens=1, output_tokens=1
        )
    assert repository.get("job").processing_metadata["model_budget"] == {
        "snapshots": {"default": 2},
        "attempts": 2,
        "reserved_input_tokens": 200,
        "reserved_output_tokens": 100,
    }


def test_stale_claim_cannot_commit_business_result(worker_outbox):
    """S19: a failed final lease check rolls back preceding business writes."""
    database, repository = worker_outbox
    first = claim(repository)
    expire(database)
    second = claim(repository)
    with pytest.raises(LookupError), database.session_factory.begin() as session:
        session.get(OutboxEventRow, "job").payload = {"uncommitted_result": True}
        repository.complete_in_session(session, "job", first.claim_epoch)
    assert repository.get("job").payload == {}
    repository.mark_published("job", claim_epoch=second.claim_epoch)
    assert repository.get("job").status == "published"


def test_result_and_completion_rollback_together(worker_outbox):
    """S18: a fault after completion cannot persist half a result transaction."""
    database, repository = worker_outbox
    event = claim(repository)
    with pytest.raises(RuntimeError), database.session_factory.begin() as session:
        repository.complete_in_session(
            session, "job", event.claim_epoch, metadata={"result": "output"}
        )
        raise RuntimeError("commit fault")
    assert repository.get("job").status == "publishing"
    assert repository.get("job").processing_metadata == {}


def test_snapshot_recomputations_are_durable_and_bounded(worker_outbox):
    """Conflict snapshots cannot bypass the total request or per-snapshot cap."""
    _, repository = worker_outbox
    event = claim(repository)
    for snapshot in ("a", "b", "c"):
        repository.reserve_model_attempt(
            "job",
            claim_epoch=event.claim_epoch,
            max_attempts=4,
            input_tokens=1,
            output_tokens=1,
            snapshot_id=snapshot,
        )
    with pytest.raises(ModelBudgetExhausted):
        repository.reserve_model_attempt(
            "job",
            claim_epoch=event.claim_epoch,
            max_attempts=4,
            input_tokens=1,
            output_tokens=1,
            snapshot_id="d",
        )


@pytest.mark.asyncio
async def test_runner_renews_while_processing(worker_outbox, monkeypatch):
    """Renewals continue during async model work and stop after transactional completion."""
    database, repository = worker_outbox
    renewals = []
    original = repository.renew_claim

    def renew(*args, **kwargs):
        """Observe renewal without replacing the actual SQL fence."""
        renewals.append(args[0])
        return original(*args, **kwargs)

    class Handler:
        """Commit a slow fixture result with the production completion primitive."""

        async def process(self, event):
            """Yield while the runner independently maintains ownership."""
            await asyncio.sleep(0.08)
            await asyncio.to_thread(
                repository.mark_published, event.event_id, claim_epoch=event.claim_epoch
            )

        def fail(self, event, reason, *, terminal):
            """Reject unexpected failure handling in this successful fixture."""
            raise AssertionError(reason)

    monkeypatch.setattr(repository, "renew_claim", renew)
    runner = MemoryJobRunner(
        repository, Handler(), destination="memory_extract", renew_seconds=0.02
    )
    assert await runner.run_once() == 1
    assert len(renewals) >= 2
    assert repository.get("job").status == "published"
