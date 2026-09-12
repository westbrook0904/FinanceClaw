"""Real PostgreSQL lease and owner-serialization probes in a disposable private schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from financeclaw.kernel.models import ModelProfile
from financeclaw.memory_worker.extraction import ExtractionHandler, worker_actor
from financeclaw.memory_worker.model import OfflineMemoryModel, StructuredMemoryModel
from financeclaw.memory_worker.prompts import MemorySuggestions
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase, normalize_database_url
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.models import EvidenceRef, MemoryActor
from financeclaw.shared.memory.repository import canonical_hash, content_hash
from financeclaw.shared.memory.tables import MemoryExtractionRow, MemoryOwnerRow
from financeclaw.shared.outbox.models import OutboxEvent, OutboxStatus
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow
from tests.stage3.support import conversation_context


@pytest.fixture
def postgres_database():
    """Require an explicit test DSN and create/drop only a freshly named test-owned schema."""
    raw = os.getenv("FINANCECLAW_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("FINANCECLAW_TEST_POSTGRES_URL is required for PostgreSQL concurrency")
    url = make_url(normalize_database_url(raw))
    admin = create_engine(url)
    schema = "stage11_worker_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    options = f"{url.query.get('options', '')} -c search_path={schema}".strip()
    scoped_url = url.set(query={**url.query, "options": options})
    database = ApplicationDatabase(scoped_url.render_as_string(hide_password=False))
    try:
        database.initialize_schema()
        yield database
    finally:
        database.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_parallel_claims_are_disjoint_and_expired_epoch_is_fenced(postgres_database):
    """S19: independent connections use SKIP LOCKED and cannot commit a replaced lease."""
    database = postgres_database
    outbox = SqlAlchemyOutboxRepository(database.session_factory)
    for number in range(4):
        outbox.enqueue(
            OutboxEvent(
                event_id=f"job-{number}",
                destination="memory_extract",
                event_type="memory.extract.part",
                aggregate_type="owner",
                aggregate_id="subject",
                tenant_id="tenant",
                subject_id="subject",
            )
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(outbox.claim_pending, destination="memory_extract", limit=2)
        second = executor.submit(outbox.claim_pending, destination="memory_extract", limit=2)
        events = (*first.result(), *second.result())
    assert len({event.event_id for event in events}) == 4
    old = events[0]
    with database.session_factory.begin() as session:
        session.get(OutboxEventRow, old.event_id).locked_until = datetime.now(UTC) - timedelta(
            seconds=1
        )
    replacement = outbox.claim_pending(destination="memory_extract", limit=1)[0]
    with pytest.raises(LookupError):
        outbox.mark_published(old.event_id, claim_epoch=old.claim_epoch)
    outbox.mark_published(replacement.event_id, claim_epoch=replacement.claim_epoch)


def test_initial_alembic_upgrade_matches_18_tables_and_downgrades(postgres_database, monkeypatch):
    """Verify the initial migration on PostgreSQL, independently of ORM create_all."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from financeclaw.shared.infrastructure.orm import Base

    database = postgres_database
    with database.engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    assert schema.startswith("stage11_worker_")
    Base.metadata.drop_all(database.engine)
    url = database.engine.url.set(query={})
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    tables = set(inspect(database.engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 18
    command.check(config)
    command.downgrade(config, "base")
    assert set(inspect(database.engine).get_table_names()) <= {"alembic_version"}


def test_parallel_parts_close_once_under_owner_lock(postgres_database):
    """S21/S23: concurrent final parts assign one readiness revision and one owner wakeup."""
    database = postgres_database
    conversations = SqlAlchemyConversationRepository(database.session_factory)
    context, message_id = conversation_context(conversations, message="分析债券期限影响")
    actor = MemoryActor(
        tenant_id=context.tenant_id,
        subject_id=context.subject_id,
        turn_id=context.turn_id,
        conversation_id=context.conversation_id,
        scopes=context.scopes,
    )
    intake = MemoryIntake(database.session_factory)
    with database.session_factory.begin() as session:
        first = intake.register_message_in_session(session, actor, message_id)
        session.add(
            ConversationMessageRow(
                message_id="second-user-evidence",
                conversation_id=actor.conversation_id,
                turn_id=actor.turn_id,
                sequence=2,
                parent_message_id=message_id,
                role="user",
                content="包括流动性风险",
                content_hash=content_hash("包括流动性风险"),
            )
        )
        session.flush()
        second = intake.register_message_in_session(session, actor, "second-user-evidence")
        session.get(ConversationTurnRow, actor.turn_id).status = "completed"
        session.add(
            ConversationMessageRow(
                message_id="final",
                conversation_id=actor.conversation_id,
                turn_id=actor.turn_id,
                sequence=3,
                role="assistant",
                content="研究已完成",
                content_hash=content_hash("研究已完成"),
            )
        )
    outbox = SqlAlchemyOutboxRepository(database.session_factory)
    profile = ModelProfile(
        profile_id="pg-memory",
        version="1.0.0",
        model="offline",
        max_tokens=2000,
        context_window_tokens=64000,
        token_estimator="utf8-bytes-v1",
    )
    model = StructuredMemoryModel(OfflineMemoryModel(), profile, outbox)
    handler = ExtractionHandler(
        database.session_factory, outbox, model, consolidation_model_version=model.fingerprint
    )
    sources = [source.evidence_ref().model_dump(mode="json") for source in (first, second)]
    closure = canonical_hash(sources)
    for index, source in enumerate(sources):
        outbox.enqueue(
            OutboxEvent(
                event_id=f"part-{index}",
                event_type="memory.extract.part",
                destination="memory_extract",
                aggregate_type="turn",
                aggregate_id=context.turn_id,
                tenant_id=actor.tenant_id,
                subject_id=actor.subject_id,
                payload={
                    "sources": [source],
                    "turn_id": actor.turn_id,
                    "conversation_id": actor.conversation_id,
                    "prepare_event_id": "prepare",
                    "closure_hash": closure,
                    "part_index": index,
                    "part_count": 2,
                    "pipeline_version": "memory/1",
                    "model_profile_version": model.fingerprint,
                    "schema_version": "memory-v1",
                    "final_message_id": "final",
                    "final_message_hash": content_hash("研究已完成"),
                },
            )
        )
    events = outbox.claim_pending(destination="memory_extract", limit=2)

    def finish(event):
        """Run each atomic part commit on a genuinely separate PostgreSQL connection."""
        refs = tuple(EvidenceRef.model_validate(value) for value in event.payload["sources"])
        handler._commit(event, worker_actor(event, refs), refs, MemorySuggestions())

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(finish, events))
    with database.session_factory() as session:
        rows = list(session.scalars(select(MemoryExtractionRow)))
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        assert [row.extraction_revision for row in rows] == [1, 1]
        assert owner.extraction_revision == 1
        assert (
            len(
                list(
                    session.scalars(
                        select(OutboxEventRow).where(
                            OutboxEventRow.destination == "memory_consolidate"
                        )
                    )
                )
            )
            == 1
        )
        assert owner.active_consolidation_event_id is not None
    # A source may belong to an earlier admitted closure that finishes after a
    # newer one. Readiness follows commit order, without changing source_seq.
    for label, original in (("older", events[0]), ("newer", events[1])):
        outbox.enqueue(
            original.model_copy(
                update={
                    "event_id": f"late-{label}",
                    "status": OutboxStatus.PENDING,
                    "claim_epoch": 0,
                    "locked_until": None,
                    "payload": {
                        **original.payload,
                        "closure_hash": canonical_hash(label),
                        "part_index": 0,
                        "part_count": 1,
                    },
                }
            )
        )
    late = {
        event.event_id: event
        for event in outbox.claim_pending(destination="memory_extract", limit=2)
    }
    finish(late["late-newer"])
    finish(late["late-older"])
    with database.session_factory() as session:
        older = session.scalar(
            select(MemoryExtractionRow).where(
                MemoryExtractionRow.closure_hash == canonical_hash("older")
            )
        )
        newer = session.scalar(
            select(MemoryExtractionRow).where(
                MemoryExtractionRow.closure_hash == canonical_hash("newer")
            )
        )
        assert older.evidence[0]["source_seq"] < newer.evidence[0]["source_seq"]
        assert older.extraction_revision > newer.extraction_revision
