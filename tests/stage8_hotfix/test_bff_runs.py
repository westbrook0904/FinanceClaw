"""HF-2 BFF admission, recovery, ownership fences and atomic Journal completion."""

import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from financeclaw.bff.application.runs.bootstrap import build_bff_runs
from financeclaw.bff.application.runs.waits import worker_binding
from financeclaw.bff.http.webhooks import webhook_router
from financeclaw.kernel.agent_server import ServerRun
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.root_repository import StaleRunLease, now
from financeclaw.shared.execution_ledger.run_tables import (
    RootRunRow,
    RunInboxRow,
)
from tests.stage7.support import settings

OWNER = {"tenant_id": "synthetic-tenant", "subject_id": "synthetic-owner"}
SCOPES = frozenset(
    {
        "market:read",
        "portfolio:review",
        "ziwei:read",
        "artifacts:read",
        "watchlist:write",
        "tools:approve",
        "workflows:approve",
    }
)


class FakeRuns:
    """A controllable native receipt inventory with no hidden operation deduplication."""

    def __init__(self, native):
        """Keep every create invocation visible to assertions."""
        self.native, self.values, self.calls = native, {}, []
        self.lose_receipt, self.hide_lookup, self.on_create = False, False, None

    async def create(self, thread_id, assistant_id, **kwargs):
        """Persist a remote receipt before optionally losing the response."""
        identity = str(uuid4())
        self.calls.append((thread_id, assistant_id, copy.deepcopy(kwargs)))
        value = {
            "run_id": identity,
            "thread_id": thread_id,
            "status": "running",
            "metadata": kwargs["metadata"],
            "created_at": now().isoformat(),
        }
        self.values[identity] = value
        if self.on_create:
            await self.on_create(value, kwargs)
        if self.lose_receipt:
            self.lose_receipt = False
            raise TimeoutError("synthetic receipt loss")
        return value

    async def get(self, thread_id, run_id):
        """Return the exact native receipt, including metadata."""
        assert self.values[run_id]["thread_id"] == thread_id
        return copy.deepcopy(self.values[run_id])

    async def list(self, thread_id, **kwargs):
        """Expose an optionally empty lookup without claiming the remote run never existed."""
        if self.hide_lookup:
            return []
        return [r for r in self.values.values() if r["thread_id"] == thread_id]


class FakeThreads:
    """Root state is separate from native receipt status, as with native interrupts."""

    def __init__(self):
        """Retain a complete thread inventory and last checkpoint states."""
        self.values, self.states = {}, {}

    async def get_state(self, thread_id, **kwargs):
        """Read only; tests set immutable completed or waiting evidence explicitly."""
        return copy.deepcopy(self.states[thread_id])


class FakeNative:
    """SDK-shaped transport for receipt loss, fencing and Journal failure injection."""

    def __init__(self):
        """Use a shared transport so reconstructed BFF applications see the same backend."""
        self._client = self
        self.runs, self.threads = FakeRuns(self), FakeThreads()
        self.cancelled = []

    async def create_thread(self, thread_id):
        """Idempotently create the fixed BFF thread."""
        self.threads.values[thread_id] = thread_id

    async def find_operation(self, *, thread_id, operation_id):
        """Require a unique operation match in the complete synthetic inventory."""
        values = await self.runs.list(thread_id)
        found = [r for r in values if r["metadata"]["operation_id"] == operation_id]
        if len(found) > 1:
            raise RuntimeError("ambiguous receipt")
        return ServerRun(found[0]["run_id"], found[0]["status"]) if found else None

    async def cancel_run(self, *, thread_id, run_id):
        """Confirm an exact attempt stopped without deleting its checkpoint."""
        self.cancelled.append(run_id)
        self.runs.values[run_id]["status"] = "interrupted"
        return True

    async def health(self):
        """Synthetic transport is always reachable."""
        return True


def config(tmp_path, **changes):
    """Avoid dotenv and real services while using production BFF release construction."""
    return settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'bff.db'}",
        database_auto_create_schema=True,
        artifact_root=str(tmp_path / "artifacts"),
        bff_callback_url="http://127.0.0.1/internal/webhooks/langgraph/langgraph-main",
        bff_webhook_token="synthetic-bff-webhook-token-at-least-32",
        bff_run_concurrency=1,
        bff_run_reconcile_seconds=0.1,
        bff_run_poll_seconds=0.05,
        **changes,
    )


@pytest.fixture
def runtime(tmp_path):
    """Create an explicitly enabled test BFF with shared durable resources."""
    native = FakeNative()
    runtime = build_bff_runs(config(tmp_path), native=native)
    yield runtime
    runtime.resources.database.close()


async def admit(runtime, *, key="start", conversation_id=None, message="HF-2 synthetic turn"):
    """Create only a new version-pinned root conversation and one user Turn."""
    if conversation_id is None:
        conversation = runtime.runs.repository.create_conversation(
            **OWNER, agent_id="finance_agent", agent_profile_version="1.5.0"
        )
        conversation_id = conversation.conversation_id
    accepted = await runtime.runs.start_turn(
        conversation_id,
        ConversationTurnRequest(message=message),
        **OWNER,
        scopes=SCOPES,
        idempotency_key=key,
    )
    return accepted


async def tick(runtime):
    """Make roots due in tests; lifecycle production code still acquires leases and epochs."""
    with runtime.runs.store.sessions.begin() as session:
        session.execute(update(RootRunRow).values(due_at=now()))
    return await runtime.lifecycle.tick()


def final_state(runtime, accepted, *, content="verified final answer"):
    """Complete the latest exact attempt with a current-Turn final assistant message."""
    native = runtime.client
    value = list(native.runs.values.values())[-1]
    value["status"] = "success"
    snapshot = runtime.runs.execution.get(accepted.run_id)["snapshot"]
    messages = [
        {"type": "human", "id": snapshot["user_message_id"], "content": "synthetic input"},
        {"type": "ai", "content": content, "tool_calls": []},
    ]
    native.threads.states[accepted.thread_id] = {
        "metadata": {"run_id": value["run_id"]},
        "checkpoint": {"checkpoint_id": str(uuid4())},
        "values": {"messages": messages},
        "tasks": [],
        "next": [],
    }
    return native.threads.states[accepted.thread_id]


def waiting_state(
    runtime, accepted, *, anchor=None, checkpoint=None, native_interrupt="question-1"
):
    """Build a code-bound Worker question at the root's pending composite Tool."""
    state = final_state(runtime, accepted)
    snapshot = runtime.runs.execution.get(accepted.run_id)["snapshot"]
    call = {
        "id": "outer-call",
        "name": "call_agent__market_research_agent",
        "args": {"task": "synthetic research"},
    }
    _, binding, _ = worker_binding(call, snapshot, runtime.runs.releases)
    payload = {
        **binding,
        "kind": "user_interaction",
        "schema_version": 1,
        "point_id": "research_scope",
        "interaction_kind": "input",
        "question": "研究区间？",
    }
    state["values"]["messages"][-1] = {"type": "ai", "content": "", "tool_calls": [call]}
    state["tasks"] = [{"interrupts": [{"id": native_interrupt, "value": payload}]}]
    state["next"] = ["tools"]
    if anchor:
        state["metadata"]["run_id"] = anchor
    if checkpoint:
        state["checkpoint"] = checkpoint
    return state


async def response(runtime, accepted, *, key="answer", answer=None):
    """Answer the exact public interaction using the same HTTP application contract."""
    status = await runtime.runs.status(accepted.run_id, **OWNER)
    item = status.pending_interactions[0]
    result = InteractionResponse(
        revision=item["revision"], kind="input", answer=answer or {"analysis_period": "2026"}
    )
    await runtime.runs.interactions.respond(
        item["interaction_id"], result, **OWNER, scopes=SCOPES, idempotency_key=key
    )
    return item, result


@pytest.mark.asyncio
async def test_admission_recovery_and_atomic_final_without_subscriber(runtime):
    """A restarted BFF owns admitted work even with no GET, SSE or Webhook."""
    accepted = await admit(runtime)
    assert not runtime.client.runs.calls
    replay = await admit(runtime, conversation_id=accepted.conversation_id)
    assert replay.idempotent_replay
    replacement = build_bff_runs(
        runtime.resources.settings,
        resources=runtime.resources,
        catalogs=runtime.releases,
        native=runtime.client,
    )
    await tick(replacement)
    assert len(runtime.client.runs.calls) == 1
    final_state(runtime, accepted)
    await tick(replacement)
    await tick(replacement)
    messages = runtime.runs.repository.list_messages(accepted.conversation_id)
    assert [m.role.value for m in messages] == ["user", "assistant"]
    assert messages[-1].content == "verified final answer"
    assert runtime.runs.execution.get(accepted.run_id)["root_run_id"] == accepted.run_id


@pytest.mark.asyncio
async def test_receipt_loss_empty_lookup_never_retries(runtime):
    """A lost accepted receipt can be found, but an empty lookup never permits another send."""
    accepted = await admit(runtime)
    runtime.client.runs.lose_receipt = True
    await tick(runtime)
    runtime.client.runs.hide_lookup = True
    await tick(runtime)
    await tick(runtime)
    assert len(runtime.client.runs.calls) == 1
    status = await runtime.runs.status(accepted.run_id, **OWNER)
    assert status.waiting_reason == "submission_uncertain"
    runtime.client.runs.hide_lookup = False
    final_state(runtime, accepted)
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "completed"
    assert len(runtime.client.runs.calls) == 1


@pytest.mark.asyncio
async def test_two_questions_share_parent_anchor_but_get_distinct_resumes(runtime):
    """The parent metadata may remain initial while active attempt and interrupt advance."""
    accepted = await admit(runtime)
    await tick(runtime)
    state = waiting_state(runtime, accepted)
    anchor, checkpoint = state["metadata"]["run_id"], state["checkpoint"]
    await tick(runtime)
    old, answer = await response(runtime, accepted)
    await tick(runtime)
    waiting_state(
        runtime, accepted, anchor=anchor, checkpoint=checkpoint, native_interrupt="question-2"
    )
    await tick(runtime)
    second = (await runtime.runs.status(accepted.run_id, **OWNER)).pending_interactions[0]
    assert second["revision"] == old["revision"] + 1
    assert second["interaction_id"] != old["interaction_id"]
    # Exact replay cannot resume the new question.
    await runtime.runs.interactions.respond(
        old["interaction_id"], answer, **OWNER, scopes=SCOPES, idempotency_key="answer"
    )
    await response(runtime, accepted, key="answer-2")
    await tick(runtime)
    final_state(runtime, accepted)
    await tick(runtime)
    assert len(runtime.client.runs.calls) == 3
    assert len(runtime.client.threads.values) == 1
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["foreign_attempt", "pending_next", "tool_call", "old_turn"])
async def test_invalid_final_evidence_cannot_write_journal(runtime, mutation):
    """A native success receipt alone cannot authorize assistant history."""
    accepted = await admit(runtime)
    await tick(runtime)
    state = final_state(runtime, accepted)
    if mutation == "foreign_attempt":
        state["metadata"]["run_id"] = "foreign"
    elif mutation == "pending_next":
        state["next"] = ["tools"]
    elif mutation == "tool_call":
        state["values"]["messages"][-1]["tool_calls"] = [{"id": "pending"}]
    else:
        state["values"]["messages"][0]["id"] = "previous-user-message"
    await tick(runtime)
    assert len(runtime.runs.repository.list_messages(accepted.conversation_id)) == 1
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "interrupted"


@pytest.mark.asyncio
async def test_journal_failure_rolls_back_attempt_and_completion(runtime, monkeypatch):
    """A failure after assistant insertion must roll back every finalization fact."""
    accepted = await admit(runtime)
    await tick(runtime)
    final_state(runtime, accepted)
    original = runtime.runs.repository.append_assistant_message

    def fail(**kwargs):
        """Inject failure inside the final transaction after the Journal write."""
        original(**kwargs)
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(runtime.runs.repository, "append_assistant_message", fail)
    await tick(runtime)
    assert len(runtime.runs.repository.list_messages(accepted.conversation_id)) == 1
    assert runtime.runs.execution.operations_for_run(accepted.run_id)[0]["status"] == "submitted"
    monkeypatch.setattr(runtime.runs.repository, "append_assistant_message", original)
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "completed"


@pytest.mark.asyncio
async def test_observe_completed_result_after_authorization_revoked(runtime):
    """Revocation stops new sends but never discards already-produced completion facts."""
    accepted = await admit(runtime)
    await tick(runtime)
    await runtime.runs.revoke_authorization(accepted.run_id, **OWNER)
    final_state(runtime, accepted)
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted", [False, True])
async def test_cancel_requires_exact_stop_and_next_turn_gets_clean_thread(runtime, submitted):
    """Prepared commands remain unsent; known attempts are cancelled before Turn finalization."""
    accepted = await admit(runtime)
    if submitted:
        await tick(runtime)
    await runtime.runs.cancel(accepted.run_id, **OWNER)
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "cancelled"
    assert len(runtime.client.cancelled) == int(submitted)
    next_turn = await admit(runtime, key="second", conversation_id=accepted.conversation_id)
    assert next_turn.thread_id != accepted.thread_id


@pytest.mark.asyncio
async def test_cancel_unknown_receipt_cannot_claim_stopped(runtime):
    """A cancellation intent cannot turn an unknown submission into a successful cancellation."""
    accepted = await admit(runtime)
    runtime.client.runs.lose_receipt = True
    await tick(runtime)
    runtime.client.runs.hide_lookup = True
    await runtime.runs.cancel(accepted.run_id, **OWNER)
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "cancellation_requested"
    runtime.client.runs.hide_lookup = False
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "cancelled"


@pytest.mark.asyncio
async def test_driver_and_epoch_fencing(runtime):
    """Foreign driver versions and stale BFF owners cannot claim or finalize new roots."""
    accepted = await admit(runtime)
    store = runtime.runs.store
    first = store.claim_due("first", lease_seconds=3)
    assert store.claim_due("second", lease_seconds=3) is None
    with store.sessions.begin() as session:
        session.get(RootRunRow, accepted.run_id).lease_until = now() - timedelta(seconds=1)
    second = store.claim_due("second", lease_seconds=3)
    assert second["epoch"] > first["epoch"]
    with pytest.raises(StaleRunLease):
        store.finish(first, delay=1)
    store.finish(second, delay=1)


@pytest.mark.asyncio
async def test_background_loop_completes_without_queries(runtime):
    """No request or callback is required after admission to keep BFF recovery alive."""
    accepted = await admit(runtime)
    await runtime.lifecycle.start()
    try:
        async with asyncio.timeout(5):
            while not runtime.client.runs.calls:
                await asyncio.sleep(0.02)
            final_state(runtime, accepted)
            while len(runtime.runs.repository.list_messages(accepted.conversation_id)) != 2:
                await asyncio.sleep(0.02)
        assert await runtime.lifecycle.healthy()
    finally:
        await runtime.lifecycle.stop()


@pytest.mark.asyncio
async def test_early_duplicate_webhook_persists_minimum_facts(runtime):
    """Ingress acknowledges durable wakeups only; values from a callback cannot become chat text."""
    accepted = await admit(runtime)
    store = runtime.runs.store
    app = FastAPI()
    app.include_router(webhook_router(store, runtime.resources.settings))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:

        async def early(value, kwargs):
            """Deliver before BFF binds the receipt to test the early-notification window."""
            path = "/internal/webhooks/langgraph/langgraph-main"
            body = {**value, "values": {"messages": [{"content": "untrusted callback text"}]}}
            assert (await client.post(path, json=body)).status_code == 401
            headers = {"Authorization": "Bearer synthetic-bff-webhook-token-at-least-32"}
            assert (await client.post(path, json=body, headers=headers)).status_code == 204
            assert (await client.post(path, json=body, headers=headers)).status_code == 204

        runtime.client.runs.on_create = early
        await tick(runtime)
    with store.sessions() as session:
        inbox = list(
            session.scalars(select(RunInboxRow).where(RunInboxRow.kind == "backend_notification"))
        )
        assert len(inbox) == 1 and inbox[0].run_id == accepted.run_id
        assert "values" not in inbox[0].payload
    assert len(runtime.runs.repository.list_messages(accepted.conversation_id)) == 1


@pytest.mark.asyncio
async def test_bad_response_and_tampered_worker_do_not_resume(runtime):
    """Schemas and invocation identity come from the frozen release, never a question payload."""
    accepted = await admit(runtime)
    await tick(runtime)
    state = waiting_state(runtime, accepted)
    payload = state["tasks"][0]["interrupts"][0]["value"]
    original = payload["invocation_id"]
    payload["invocation_id"] = "foreign"
    await tick(runtime)
    assert not (await runtime.runs.status(accepted.run_id, **OWNER)).pending_interactions
    payload["invocation_id"] = original
    await tick(runtime)
    with pytest.raises(ExecutionConflict):
        await response(runtime, accepted, answer={"unpublished": "value"})
    assert len(runtime.client.runs.calls) == 1
