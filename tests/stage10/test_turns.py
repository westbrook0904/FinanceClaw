"""Durable admission, sending rights, exact observations and fenced cancellation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from financeclaw.api.application.turns.backend import NativeRuns
from financeclaw.api.application.turns.commands import CommandService
from financeclaw.api.application.turns.results import ResultService
from financeclaw.shared.conversation.repository import ConversationConflict, IdempotencyConflict
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.budget import snapshot_context
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from financeclaw.shared.turns.types import (
    ExecutionConflict,
    StaleTurnLease,
    TurnNotFound,
    digest,
    now,
)


def submitted(service, accepted):
    """Provide the submitted boundary for this test scenario."""
    lease = service.store.claim_due("test")
    commands = CommandService(service, None)
    turn, command = commands.read(lease)
    commands.claim_send(lease, command["command_id"])
    commands.bind(lease, command["command_id"], str(uuid4()))
    return lease, *commands.read(lease)


def test_admission_is_atomic_and_replay_has_one_identity(service, admit):
    """Admission is atomic and replay has one identity."""
    first = admit(key="one")
    again = admit(key="one", conversation_id=first.conversation_id)
    assert again.turn_id == first.turn_id and again.idempotent_replay
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1
        assert session.scalar(select(func.count()).select_from(TurnCommandRow)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
        turn = session.get(ConversationTurnRow, first.turn_id)
        assert turn.current_command_id and turn.release_hash == digest(turn.release_snapshot)
    with pytest.raises(IdempotencyConflict):
        admit(key="one", conversation_id=first.conversation_id, message="different")
    with pytest.raises(ConversationConflict):
        admit(key="two", conversation_id=first.conversation_id)
    with pytest.raises(TurnNotFound):
        service.assert_owned(first.turn_id, tenant_id="other", subject_id="user")


def test_failed_admission_rolls_back_every_runtime_fact(service, admit):
    """Failed admission rolls back every runtime fact."""
    with pytest.raises(ValueError):
        admit(notification_address={"untrusted": True})
    with service.sessions() as session:
        for table in (ConversationTurnRow, TurnCommandRow, ConversationMessageRow):
            assert session.scalar(select(func.count()).select_from(table)) == 0


def test_concurrent_admission_has_one_winner(service, admit):
    """Concurrent admission has one winner."""
    first = admit()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: admit(key=first.conversation_id, conversation_id=first.conversation_id),
                range(16),
            )
        )
    assert {result.turn_id for result in results} == {first.turn_id}


def test_database_rejects_cross_turn_command_binding(service, admit):
    """Database rejects cross turn command binding."""
    first, second = admit(), admit()
    with pytest.raises(IntegrityError), service.sessions.begin() as session:
        a, b = (
            session.get(ConversationTurnRow, first.turn_id),
            session.get(ConversationTurnRow, second.turn_id),
        )
        a.current_command_id = b.current_command_id


def test_budget_counts_real_attempts_atomically(service, admit):
    """Budget counts real attempts atomically."""
    accepted = admit()
    lease, turn, command = submitted(service, accepted)
    context = snapshot_context(turn["release_snapshot"], command_id=command["command_id"])
    service.execution.verify_context(context)
    with service.sessions.begin() as session:
        row = session.get(ConversationTurnRow, accepted.turn_id)
        row.release_snapshot = {
            **row.release_snapshot,
            "limits": {**row.release_snapshot["limits"], "model": 3},
        }

    def consume(_):
        """Provide the consume boundary for this test scenario."""
        try:
            service.execution.consume(accepted.turn_id, "model")
            return True
        except ExecutionConflict:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(16))) == 3
    with service.sessions.begin() as session:
        row = session.get(ConversationTurnRow, accepted.turn_id)
        row.grant_revoked = True
    with pytest.raises(ExecutionConflict):
        service.execution.verify_context(context)


@pytest.mark.asyncio
async def test_unknown_submission_never_resends_after_lease_recovery(service, admit):
    """Unknown submission never resends after lease recovery."""
    admit()
    lease = service.store.claim_due("first")
    calls = []

    class Native:
        """Provide the Native boundary for this test scenario."""

        async def submit(self, turn, command):
            """Provide the submit boundary for this test scenario."""
            calls.append(command["command_id"])
            raise ConnectionError("receipt lost")

        async def lookup(self, turn, command):
            """Provide the lookup boundary for this test scenario."""
            return None

    commands = CommandService(service, Native())
    turn, command = commands.read(lease)
    with pytest.raises(ConnectionError):
        await commands.reconcile(lease, turn, command)
    service.store.release(lease, delay=0)
    replacement = service.store.claim_due("second")
    await commands.reconcile(replacement, *commands.read(replacement))
    assert len(calls) == 1
    assert commands.read(replacement)[1]["state"] == "uncertain"
    with pytest.raises(StaleTurnLease):
        ResultService(service).blocked(lease, "stale")


@pytest.mark.asyncio
async def test_lookup_exhausts_pages_and_rejects_duplicate_receipts(service, admit):
    """Lookup exhausts pages and rejects duplicate receipts."""
    accepted = admit()
    lease, turn, command = submitted(service, accepted)
    wanted = {
        "thread_id": turn["thread_id"],
        "run_id": command["native_run_id"],
        "metadata": {
            "turn_id": turn["turn_id"],
            "command_id": command["command_id"],
            "request_hash": command["request_hash"],
            "release_hash": turn["release_hash"],
        },
    }
    rows = [{"metadata": {}} for _ in range(204)] + [wanted]
    offsets = []

    async def list_runs(thread_id, *, limit, offset):
        """Provide the list runs boundary for this test scenario."""
        offsets.append(offset)
        return rows[offset : offset + limit]

    native = NativeRuns(SimpleNamespace(runs=SimpleNamespace(list=list_runs)), None, None)
    assert await native.lookup(turn, command) == command["native_run_id"]
    assert offsets == [0, 100, 200]
    rows.append(wanted)
    with pytest.raises(ExecutionConflict):
        await native.lookup(turn, command)


def test_terminal_journal_outbox_and_state_are_atomic(service, admit, monkeypatch):
    """Terminal journal outbox and state are atomic."""
    accepted = admit()
    lease, turn, command = submitted(service, accepted)
    observation = {
        "status": "completed",
        "content": "final answer",
        "checkpoint": {"checkpoint_id": "final"},
    }

    def fail(session, turn):
        """Provide the fail boundary for this test scenario."""
        raise RuntimeError("notification write failed")

    with monkeypatch.context() as patch:
        patch.setattr("financeclaw.shared.notifications.facts.record_progress", fail)
        with pytest.raises(RuntimeError):
            ResultService(service).apply(lease, command["command_id"], observation)
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 0
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
    ResultService(service).apply(lease, command["command_id"], observation)
    ResultService(service).apply(lease, command["command_id"], observation)
    value = service.read_snapshot(accepted.turn_id, tenant_id="tenant", subject_id="user")
    assert value.status == "completed" and value.output["messages"][0]["content"] == "final answer"
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 2


@pytest.mark.asyncio
async def test_cancel_unsent_does_not_create_native_run(service, admit):
    """Cancel unsent does not create native run."""
    accepted = admit()
    await service.cancel(
        accepted.turn_id, tenant_id="tenant", subject_id="user", command_id="cancel"
    )
    lease = service.store.claim_due("test")
    await service.lifecycle._step(lease)
    assert (
        await service.status(accepted.turn_id, tenant_id="tenant", subject_id="user")
    ).status == "cancelled"
    with service.sessions() as session:
        assert session.scalar(select(TurnCommandRow.native_run_id)) is None


def test_late_lease_cannot_reacquire_or_overwrite_wakeup(service, admit):
    """Late lease cannot reacquire or overwrite wakeup."""
    accepted = admit()
    lease = service.store.claim_due("old")
    with service.sessions.begin() as session:
        row = session.get(ConversationTurnRow, accepted.turn_id)
        row.lease_until = now() - timedelta(seconds=1)
        row.next_action_at = now() - timedelta(seconds=1)
    assert not service.store.renew(lease)
    replacement = service.store.claim_due("new")
    with pytest.raises(StaleTurnLease):
        service.store.release(lease, delay=30)
    with service.sessions.begin() as session:
        service.store.wake(session, session.get(ConversationTurnRow, accepted.turn_id))
    service.store.release(replacement, delay=30)
    assert service.store.claim_due("next") is not None
