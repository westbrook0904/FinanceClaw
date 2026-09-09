"""通知意图与受理、Journal、进度共享事务；不依赖 BFF 或网络。"""

from sqlalchemy import select

from financeclaw.kernel.notifications import NotificationAddress
from financeclaw.shared.conversation.tables import (
    ChannelConversationBindingRow,
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
)
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.execution_ledger.run_tables import RunAuthorizationRow
from financeclaw.shared.notifications.tables import NotificationEventRow, NotificationTargetRow


def bind_target(session, root, address, *, tenant_id, subject_id, evidence, replay=False):
    """原受理事务核对完整渠道身份及来源摘要，重放不得新增或修改订阅。"""
    address = NotificationAddress.model_validate(address)
    values = address.model_dump(mode="json")
    target = session.scalar(
        select(NotificationTargetRow).where(NotificationTargetRow.run_id == root.run_id)
    )
    if replay:
        if target is None or target.address != values:
            raise ExecutionConflict("notification subscription cannot change on replay")
        return
    binding = session.scalar(
        select(ChannelConversationBindingRow).where(
            ChannelConversationBindingRow.conversation_id == root.conversation_id,
            ChannelConversationBindingRow.channel == address.channel,
            ChannelConversationBindingRow.app_id == address.app_id,
            ChannelConversationBindingRow.tenant_key == address.tenant_key,
            ChannelConversationBindingRow.external_chat_id == address.chat_id,
            ChannelConversationBindingRow.external_user_id == address.open_id,
        )
    )
    if (
        binding is None
        or (tenant_id, subject_id) != (f"feishu:{address.tenant_key}", f"feishu:{address.open_id}")
        or evidence.source != "feishu"
        or evidence.source_hash
        != digest(
            [
                address.app_id,
                address.tenant_key,
                address.open_id,
                address.chat_id,
                address.message_id,
            ]
        )
    ):
        raise ExecutionConflict("notification target lacks verified channel binding")
    session.add(
        NotificationTargetRow(
            target_id=digest([root.run_id, "notification"]),
            run_id=root.run_id,
            binding_id=binding.binding_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            app_id=address.app_id,
            address=values,
        )
    )
    session.flush()


def target_valid(session, target) -> bool:
    """本地撤销或身份、会话重绑定立即阻止后续分片；已在途网络调用仍需回执。"""
    binding = session.get(ChannelConversationBindingRow, target.binding_id)
    if not target.active or binding is None or target.delivery_mode != "text_reply_v1":
        return False
    address = target.address
    conversation = session.get(ConversationRow, binding.conversation_id)
    turn = session.scalar(
        select(ConversationTurnRow).where(ConversationTurnRow.run_id == target.run_id)
    )
    return bool(
        conversation
        and turn
        and conversation.status == "active"
        and turn.conversation_id == conversation.conversation_id
        and (conversation.tenant_id, conversation.subject_id)
        == (target.tenant_id, target.subject_id)
        and (
            binding.channel,
            binding.app_id,
            binding.tenant_key,
            binding.external_user_id,
            binding.external_chat_id,
        )
        == (
            address["channel"],
            target.app_id,
            address["tenant_key"],
            address["open_id"],
            address["chat_id"],
        )
    )


def record_progress(session, root) -> None:
    """仅根终态、待决定和需处理停顿产生通知；无订阅的 API 任务不自动补发。"""
    target = session.scalar(
        select(NotificationTargetRow).where(NotificationTargetRow.run_id == root.run_id)
    )
    if target is None:
        return
    projection = root.projection
    status, reason = projection["status"], projection.get("waiting_reason")
    items = [
        item for item in projection.get("pending_interactions", ()) if item["status"] == "pending"
    ]
    payload = {"run_id": root.run_id, "status": status, "waiting_reason": reason}
    if status in {"completed", "failed", "cancelled"}:
        kind, key = "terminal", "terminal"
        if status == "completed":
            content = session.scalar(
                select(ConversationMessageRow.content)
                .join(ConversationTurnRow)
                .where(
                    ConversationTurnRow.run_id == root.run_id,
                    ConversationMessageRow.role == "assistant",
                    ConversationMessageRow.parent_message_id.is_(None),
                )
            )
            if content is None:
                raise ExecutionConflict("completed notification requires Journal answer")
            payload["content"] = content
    elif items and reason != "authorization_required":
        kind = "interaction"
        payload["interactions"] = items
        key = [kind, [(item["interaction_id"], item["revision"]) for item in items]]
    elif status in {"interrupted", "cancellation_requested"} and reason not in {
        None,
        "delivery_pending",
    }:
        kind = "attention"
        grant = session.get(RunAuthorizationRow, root.run_id)
        key = [kind, reason, grant.revision if grant else 0]
    else:
        return
    event_id = digest([target.target_id, key])
    if session.get(NotificationEventRow, event_id) is None:
        session.add(
            NotificationEventRow(
                event_id=event_id,
                target_id=target.target_id,
                revision=root.revision,
                kind=kind,
                payload=payload,
            )
        )


def require_schema(sessions) -> None:
    """BFF 与发送器必须具备通知表；不能静默丢弃已订阅任务的交付责任。"""
    from sqlalchemy import inspect

    from financeclaw.shared.notifications.tables import (
        NotificationDeliveryRow,
        NotificationSenderRow,
    )

    with sessions() as session:
        inspector = inspect(session.get_bind())
        for row in (
            NotificationTargetRow,
            NotificationEventRow,
            NotificationDeliveryRow,
            NotificationSenderRow,
        ):
            if not inspector.has_table(row.__tablename__) or not set(
                row.__table__.columns.keys()
            ).issubset({column["name"] for column in inspector.get_columns(row.__tablename__)}):
                raise RuntimeError("application database migration is required")
