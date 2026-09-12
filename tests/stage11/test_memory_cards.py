"""Independent candidate delivery and confirmation reuse real Feishu business boundaries."""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from financeclaw.api.application.feishu_card_actions import button_values
from financeclaw.shared.channels.feishu.memory_cards import render_memory_card
from financeclaw.shared.memory.models import MemoryActor, MemoryMutation
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import MemoryRepository, source_snapshot
from financeclaw.shared.memory.tables import MemorySourceRow
from financeclaw.shared.notifications.tables import NotificationDeliveryRow as Delivery
from financeclaw.shared.notifications.tables import NotificationEventRow as Event
from financeclaw.shared.notifications.tables import NotificationTargetRow as Target
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from tests.stage8.test_notifications import Gateway
from tests.stage8_hotfix.test_feishu_cards import publish
from tests.stage8_hotfix.test_feishu_clarification import OWNER, Replies, channel, message
from tests.stage10.runtime import SCOPES, final_state, tick
from tests.stage10.runtime import runtime as runtime


class SeparateCardGateway(Gateway):
    """Simulate distinct CardKit instances so accidental task-card reuse is visible."""

    def __init__(self):
        """Keep the external simulated instance counter independent of delivery keys."""
        super().__init__()
        self.cards = 0

    async def create_card(self, content):
        """Return a new instance only for an actual first delivery."""
        self.cards += 1
        return f"card-{self.cards}"


@pytest.mark.asyncio
async def test_candidate_after_completed_turn_uses_separate_card_and_no_resume(runtime):
    """S26/S27/S30: completed task cards cannot be overwritten or used to approve memory."""
    service = channel(runtime)
    assert await service.process(message("请记住我的投资目标是三年后购房"), Replies()) == "accepted"
    with runtime.turns.sessions() as session:
        turn = session.scalar(select(ConversationTurnRow))
        accepted = SimpleNamespace(turn_id=turn.turn_id, thread_id=turn.thread_id)
    gateway = SeparateCardGateway()
    await publish(runtime, gateway)
    await tick(runtime)
    final_state(runtime, accepted)
    await tick(runtime)
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "completed"
    await publish(runtime, gateway)
    with runtime.turns.sessions() as session:
        original = session.scalar(select(Target))
        task_card = (original.card_id, original.card_message_id, original.card_sequence)
        source = source_snapshot(
            session.scalar(
                select(MemorySourceRow).where(MemorySourceRow.turn_id == accepted.turn_id)
            )
        )
        command_count = session.scalar(select(func.count()).select_from(TurnCommandRow))
    actor = MemoryActor(
        **OWNER,
        scopes=SCOPES,
        kind="tool",
        turn_id=accepted.turn_id,
        conversation_id=source.conversation_id,
        agent_id="finance_agent",
    )
    proposal = MemoryMutationService(runtime.turns.sessions).apply(
        actor,
        MemoryMutation(
            mutation_id="candidate-after-completion",
            kind="profile",
            field="investment_goal",
            content="三年后购房",
            evidence=(source.evidence_ref(),),
        ),
    )
    assert proposal.status == "proposed"
    await publish(runtime, gateway)
    with runtime.turns.sessions() as session:
        target = session.scalar(select(Target))
        assert (target.card_id, target.card_message_id, target.card_sequence) == task_card
        event = session.scalar(select(Event).where(Event.kind == "memory_candidates"))
        delivery = session.scalar(select(Delivery).where(Delivery.event_id == event.event_id))
        assert delivery.status == "sent" and delivery.card_id != task_card[0]
        assert delivery.message_id != task_card[1] and delivery.target_message_id is None
        value = next(
            item
            for item in button_values(render_memory_card(event.event_id, event.payload))
            if item["decision"] == "approve"
        )
        callback = {
            "header": {
                "app_id": "app",
                "tenant_key": "tenant",
                "event_type": "card.action.trigger",
                "event_id": "memory-click",
            },
            "event": {
                "operator": {"open_id": "user", "tenant_key": "tenant"},
                "context": {"open_chat_id": "chat", "open_message_id": delivery.message_id},
                "action": {"value": value},
            },
        }
    result = await service.card_actions.handle(callback)
    assert result["toast"]["content"] == "记忆已确认生效", result
    callback["header"]["event_id"] = "second-physical-click"
    assert await channel(runtime).card_actions.handle(callback) == result
    with runtime.turns.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TurnCommandRow)) == command_count
        target = session.scalar(select(Target))
        assert (target.card_id, target.card_message_id, target.card_sequence) == task_card
    assert (
        len(
            MemoryRepository(runtime.turns.sessions).list_records(
                actor.model_copy(update={"kind": "user"}), kind="profile"
            )
        )
        == 1
    )
    callback["event"]["context"]["open_message_id"] = task_card[1]
    assert (await service.card_actions.handle(callback))["toast"]["type"] == "error"
