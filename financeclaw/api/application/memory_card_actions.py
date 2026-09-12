"""Accept independent memory decisions from a verified Feishu card callback."""

from sqlalchemy import select

from financeclaw.shared.channels.feishu.cards import toast
from financeclaw.shared.channels.feishu.memory_cards import render_memory_card
from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.notifications.facts import target_valid
from financeclaw.shared.notifications.tables import NotificationDeliveryRow, NotificationTargetRow
from financeclaw.shared.turns.types import digest


def accept_memory_card(actions, session, view, *, tenant, operator, context, value, form):
    """Validate the independent delivery, exact displayed action and current user scopes."""
    from financeclaw.api.application.feishu_card_actions import button_values

    target = session.get(NotificationTargetRow, view.target_id)
    if (
        target is None
        or target.app_id != actions.app_id
        or target.tenant_id != f"feishu:{tenant}"
        or target.subject_id != f"feishu:{operator['open_id']}"
        or target.address["chat_id"] != context["open_chat_id"]
        or not target_valid(session, target, require_active=False)
        or form
        or value not in list(button_values(render_memory_card(view.event_id, view.payload)))
    ):
        raise ValueError("memory decision does not match its reviewed channel view")
    delivered = session.scalar(
        select(NotificationDeliveryRow).where(
            NotificationDeliveryRow.event_id == view.event_id,
            NotificationDeliveryRow.status == "sent",
            NotificationDeliveryRow.message_id == context["open_message_id"],
        )
    )
    if delivered is None:
        raise ValueError("memory candidate delivery is unconfirmed or belongs to another message")
    actor = MemoryActor(
        tenant_id=target.tenant_id, subject_id=target.subject_id, scopes=actions.scopes, kind="user"
    )
    if (
        not actions.runs.settings.memory_enabled
        and value["decision"] == "approve"
        and view.payload["operation"] != "forget"
    ):
        raise PermissionError("memory is disabled by deployment policy")
    result = MemoryMutationService(actions.runs.store.sessions).decide_in_session(
        session,
        actor,
        value["candidate_id"],
        value["decision"],
        "feishu:memory:" + digest([view.event_id, value["decision"]]),
        value["revision"],
        value["content_hash"],
    )
    return toast(
        "记忆提案已拒绝"
        if result.status == "rejected"
        else "已遗忘该记忆"
        if result.status == "forgotten"
        else "记忆已确认生效"
    )
