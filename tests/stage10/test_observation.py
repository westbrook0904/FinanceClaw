"""Native evidence, human decisions and cancellation across successive commands."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from financeclaw.shared.conversation.repository import ConversationConflict
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow
from financeclaw.shared.turns.types import InteractionConflict, now
from tests.stage10.runtime import OWNER, SCOPES, admit, answer, final_state, question, tick
from tests.stage10.runtime import runtime as runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["foreign_attempt", "pending_next", "tool_call", "old_turn", "empty", "foreign_receipt"],
)
async def test_invalid_final_proof_never_enters_journal(runtime, mutation):
    """Success requires current command, current input and a final answer without pending work."""
    accepted = await admit(runtime)
    await tick(runtime)
    state = final_state(runtime, accepted)
    if mutation == "foreign_attempt":
        state["metadata"]["run_id"] = "foreign"
    elif mutation == "pending_next":
        state["next"] = ["tools"]
    elif mutation == "tool_call":
        state["values"]["messages"][-1]["tool_calls"] = [{"id": "pending"}]
    elif mutation == "old_turn":
        state["values"]["messages"][0]["id"] = "old"
    elif mutation == "empty":
        state["values"]["messages"][-1]["content"] = ""
    else:
        next(iter(runtime.client.values.values()))["metadata"]["command_id"] = "foreign"
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status != "completed"
    assert len(runtime.turns.journal.list_messages(accepted.conversation_id)) == 1


@pytest.mark.asyncio
async def test_success_interrupt_and_two_answers_keep_one_turn(runtime):
    """Native success with pending interrupts creates decisions, not premature completion."""
    accepted = await admit(runtime)
    await tick(runtime)
    first = await question(runtime, accepted)
    decided = await answer(runtime, first)
    with runtime.turns.sessions() as session:
        origin = session.get(InteractionRow, first["interaction_id"])
        assert origin.origin_command_id != origin.resume_command_id
        assert (
            session.get(ConversationTurnRow, accepted.turn_id).current_command_id
            == origin.resume_command_id
        )
    assert await answer(runtime, first) == decided
    await tick(runtime)
    second = await question(runtime, accepted, interrupt_id="second")
    assert second["revision"] == first["revision"] + 1
    assert await answer(runtime, first) == decided
    with pytest.raises(InteractionConflict):
        await answer(runtime, first, text="农历")
    await answer(runtime, second, key="answer-2")
    await tick(runtime)
    final_state(runtime, accepted)
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "completed"
    assert len(runtime.client.calls) == 3
    assert len({call["thread_id"] for call in runtime.client.calls}) == 1
    with runtime.turns.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1
        assert session.get(ConversationTurnRow, accepted.turn_id).command_calls == 3


@pytest.mark.asyncio
async def test_expired_interaction_and_changed_wait_cannot_resume(runtime):
    """An expired answer or moved native checkpoint cannot start another graph attempt."""
    accepted = await admit(runtime)
    await tick(runtime)
    item = await question(runtime, accepted)
    with runtime.turns.sessions.begin() as session:
        row = session.get(InteractionRow, item["interaction_id"])
        row.expires_at = now() - timedelta(seconds=1)
    with pytest.raises(InteractionConflict):
        await answer(runtime, item)
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).reason == "interaction_expired"
    assert len(runtime.client.calls) == 1


@pytest.mark.asyncio
async def test_rejection_is_durable_and_scope_is_required(runtime):
    """Approval permissions are checked independently, and rejection closes later writes."""
    accepted = await admit(runtime)
    await tick(runtime)
    item = await question(runtime, accepted, approval=True)
    with pytest.raises(PermissionError):
        await answer(runtime, item, scopes=SCOPES - {"tools:approve"}, decision="approve")
    await answer(runtime, item, decision="reject")
    with runtime.turns.sessions() as session:
        turn = session.get(ConversationTurnRow, accepted.turn_id)
        assert turn.side_effects_denied
        assert session.get(InteractionRow, item["interaction_id"]).status == "rejected"
    await tick(runtime)
    assert len(runtime.client.calls) == 2
    assert "reject" in str(runtime.client.calls[-1]["command"])


@pytest.mark.asyncio
async def test_completed_observation_survives_grant_revocation(runtime):
    """Revoking new work must not hide an already finished native result."""
    accepted = await admit(runtime)
    await tick(runtime)
    final_state(runtime, accepted)
    await runtime.turns.revoke_authorization(
        accepted.turn_id, **OWNER, command_id="revoke", expected_grant_revision=1
    )
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted", [False, True])
async def test_cancellation_requires_stop_and_next_turn_has_clean_thread(runtime, submitted):
    """A confirmed cancelled task releases the conversation, using a fresh native thread next."""
    accepted = await admit(runtime)
    if submitted:
        await tick(runtime)
    before = runtime.turns.store.owned(accepted.turn_id, **OWNER)
    result = await runtime.turns.cancel(accepted.turn_id, **OWNER, command_id="cancel")
    assert result.status == "cancelling"
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "cancelled"
    next_turn = await admit(runtime, conversation_id=accepted.conversation_id)
    assert runtime.turns.store.owned(next_turn.turn_id, **OWNER)["thread_id"] != before["thread_id"]
    assert len(runtime.client.cancelled) == int(submitted)


@pytest.mark.asyncio
async def test_cancel_unknown_submission_keeps_conversation_busy(runtime, monkeypatch):
    """Absence from run lookup cannot prove that an uncertain submission never executed."""

    async def lost(*args, **kwargs):
        """Simulate a failed transport without a trustworthy native receipt."""
        raise TimeoutError("unknown remote outcome")

    monkeypatch.setattr(runtime.client, "create", lost)
    accepted = await admit(runtime)
    await tick(runtime)
    await runtime.turns.cancel(accepted.turn_id, **OWNER, command_id="cancel")
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "cancelling"
    with pytest.raises(ConversationConflict):
        await admit(runtime, conversation_id=accepted.conversation_id)
    assert not runtime.client.cancelled


@pytest.mark.asyncio
async def test_rejection_reentry_is_bound_to_current_command_and_original_tool(runtime):
    """A rejected child may return through its wrapper; another write or invocation stays closed."""
    from threading import BoundedSemaphore
    from types import SimpleNamespace

    from financeclaw.agent_server.middleware.execution_middleware import ExecutionBudgetMiddleware
    from financeclaw.kernel.tools import SideEffect
    from financeclaw.shared.turns.budget import snapshot_context
    from financeclaw.shared.turns.types import ExecutionConflict

    accepted = await admit(runtime)
    await tick(runtime)
    item = await question(runtime, accepted, approval=True)
    with runtime.turns.sessions.begin() as session:
        decision = session.get(InteractionRow, item["interaction_id"])
        decision.request = {
            **decision.request,
            "invocation": {
                "root_tool_call_id": "original-call",
                "invocation_id": "original-invocation",
            },
        }
    await answer(runtime, item, decision="reject")
    await tick(runtime)
    execution = runtime.turns.execution
    row = execution.get(accepted.turn_id)
    context = snapshot_context(row["release_snapshot"], command_id=row["current_command_id"])
    assert execution.rejected_invocation(
        context, "original-call", invocation_id="original-invocation"
    )
    assert not execution.rejected_invocation(context, "original-call", invocation_id="changed")
    assert not execution.rejected_invocation(context, "another-call")
    assert not execution.rejected_invocation(
        context.model_copy(update={"command_id": "previous-command"}), "original-call"
    )
    managed = SimpleNamespace(governance=SimpleNamespace(side_effect=SideEffect.COMPOSITE))
    middleware = ExecutionBudgetMiddleware(
        execution, SimpleNamespace(resolve=lambda name: managed), BoundedSemaphore(1)
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        tool_call={"name": "wrapper", "id": "original-call"},
    )
    middleware._consume(request, "tool")
    request.tool_call["id"] = "another-call"
    with pytest.raises(ExecutionConflict, match="rejected"):
        middleware._consume(request, "tool")
    request.tool_call["id"] = "original-call"
    managed.governance.side_effect = SideEffect.WRITE
    with pytest.raises(ExecutionConflict, match="rejected"):
        middleware._consume(request, "tool")
