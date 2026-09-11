"""最终 Journal 提交事务中的历史索引事件；共享层不加载模型或 Store。"""

from financeclaw.shared.outbox.tables import OutboxEventRow

HISTORY_INDEX_DESTINATION = "history_index"


def enqueue_history_index(session, turn, user, answer):
    """只写可信来源标识与 hash，消费者在执行端读取原文。"""
    if user is None:
        raise ValueError("completed Turn is missing its user message")
    identity = f"history-index:{turn.turn_id}"
    if session.get(OutboxEventRow, identity) is not None:
        return
    session.add(
        OutboxEventRow(
            event_id=identity,
            event_type="history.index.requested",
            destination=HISTORY_INDEX_DESTINATION,
            aggregate_type="conversation_turn",
            aggregate_id=turn.turn_id,
            tenant_id=turn.tenant_id,
            subject_id=turn.subject_id,
            payload={
                "conversation_id": turn.conversation_id,
                "turn_id": turn.turn_id,
                "run_id": turn.run_id,
                "version": 1,
                "sources": [
                    {"message_id": item.message_id, "content_hash": item.content_hash}
                    for item in (user, answer)
                ],
            },
            available_at=answer.created_at,
        )
    )


def requeue_history(
    sessions,
    *,
    conversation_id: str,
    tenant_id: str,
    subject_id: str,
    limit: int = 100,
    offset: int = 0,
    apply: bool = False,
) -> dict:
    """按会话分页重建已完成 Turn 的派生索引，不重放审计或记忆删除任务。"""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from financeclaw.shared.conversation.tables import (
        ConversationMessageRow,
        ConversationRow,
        ConversationTurnRow,
    )

    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("invalid history rebuild page")
    results = []
    now = datetime.now(UTC)
    with sessions.begin() as session:
        conversation = session.scalar(
            select(ConversationRow).where(
                ConversationRow.conversation_id == conversation_id,
                ConversationRow.tenant_id == tenant_id,
                ConversationRow.subject_id == subject_id,
            )
        )
        if conversation is None:
            raise LookupError("conversation was not found for owner")
        turns = list(
            session.scalars(
                select(ConversationTurnRow)
                .where(
                    ConversationTurnRow.conversation_id == conversation_id,
                    ConversationTurnRow.status == "completed",
                )
                .order_by(ConversationTurnRow.created_at, ConversationTurnRow.turn_id)
                .limit(limit)
                .offset(offset)
            )
        )
        for turn in turns:
            sources = list(
                session.scalars(
                    select(ConversationMessageRow)
                    .where(
                        ConversationMessageRow.turn_id == turn.turn_id,
                        ConversationMessageRow.parent_message_id.is_(None),
                        ConversationMessageRow.visible.is_(True),
                    )
                    .order_by(ConversationMessageRow.sequence)
                )
            )
            if len(sources) != 2 or [source.role for source in sources] != ["user", "assistant"]:
                results.append({"turn_id": turn.turn_id, "status": "source_unavailable"})
                continue
            event_id = f"history-index:{turn.turn_id}"
            event = session.scalar(
                select(OutboxEventRow).where(OutboxEventRow.event_id == event_id).with_for_update()
            )
            if event and event.status == "publishing":
                results.append({"turn_id": turn.turn_id, "status": "consumer_in_progress"})
                continue
            if apply:
                if event is None:
                    enqueue_history_index(session, turn, *sources)
                else:
                    event.status = "pending"
                    event.attempts = 0
                    event.claim_epoch += 1
                    event.available_at = now
                    event.locked_until = event.published_at = event.last_error = None
            results.append({"turn_id": turn.turn_id, "status": "queued" if apply else "eligible"})
    return {"turns": results, "next_offset": offset + limit if len(turns) == limit else None}
