"""根进度持久事件的只读 SSE：独立游标、有限回放、缺口回到 Journal 快照。"""

import asyncio

from sqlalchemy import and_, select

from financeclaw.coordination.application.run_service import RunNotFound
from financeclaw.coordination.application.streaming import (
    completed_stream_event,
    failed_stream_event,
    interrupted_stream_event,
)
from financeclaw.kernel.responses import StreamEvent
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.coordination_tables import (
    CoordinatedRunRow,
    RunProgressEventRow,
)


def read_progress(sessions, run_id, tenant_id, subject_id, cursor):
    """一次 SQL 读取 revision、投影和 Journal，防止新答案搭配旧完成快照。"""
    with sessions() as session:
        result = session.execute(
            select(
                CoordinatedRunRow.revision,
                CoordinatedRunRow.projection,
                ConversationMessageRow.content,
            )
            .join(ConversationTurnRow, ConversationTurnRow.run_id == CoordinatedRunRow.run_id)
            .outerjoin(
                ConversationMessageRow,
                and_(
                    ConversationMessageRow.turn_id == ConversationTurnRow.turn_id,
                    ConversationMessageRow.role == "assistant",
                    ConversationMessageRow.parent_message_id.is_(None),
                ),
            )
            .where(
                CoordinatedRunRow.run_id == run_id,
                ConversationTurnRow.tenant_id == tenant_id,
                ConversationTurnRow.subject_id == subject_id,
            )
        ).one_or_none()
        if result is None:
            raise RunNotFound("run not found")
        revision, projection, content = result
        events = (
            list(
                session.scalars(
                    select(RunProgressEventRow)
                    .where(
                        RunProgressEventRow.run_id == run_id,
                        RunProgressEventRow.revision > (cursor or 0),
                        RunProgressEventRow.revision < revision,
                    )
                    .order_by(RunProgressEventRow.revision)
                    .limit(256)
                )
            )
            if cursor is not None
            else []
        )
        return revision, projection, content, [(event.revision, event.payload) for event in events]


async def stream_progress(admission, run_id, *, tenant_id, subject_id, last_event_id):
    """回放只含安全进度；当前交互与最终文本始终以最新快照为准，不回放 token。"""
    cursor, reset = None, None
    if last_event_id is not None:
        try:
            identity, raw = last_event_id.rsplit(":", 1)
            cursor = int(raw)
            if identity != run_id or not 0 <= cursor <= 2_147_483_647:
                raise ValueError("invalid cursor")
        except ValueError:
            cursor, reset = None, "invalid_cursor"
    first = True
    while True:
        revision, projection, content, events = await asyncio.to_thread(
            read_progress, admission.store.sessions, run_id, tenant_id, subject_id, cursor
        )
        if cursor is not None and cursor > revision:
            reset = "future_cursor"
        elif cursor is not None and revision > cursor:
            expected = revision - cursor - 1
            if len(events) != expected or any(
                number != cursor + i + 1 for i, (number, _) in enumerate(events)
            ):
                reset = "history_unavailable"
            else:
                for number, payload in events:
                    yield StreamEvent(
                        id=f"{run_id}:{number}",
                        event="run.progress",
                        data={**payload, "revision": number, "replay": True},
                    )
        status = projection["status"]
        if first or cursor != revision:
            if status == "completed":
                event = completed_stream_event(
                    run_id, {"messages": [{"type": "assistant", "content": content}]}
                )
            elif status == "failed":
                event = failed_stream_event(run_id)
            elif status == "interrupted":
                event = interrupted_stream_event(
                    run_id,
                    waiting_reason=projection.get("waiting_reason"),
                    pending_interactions=tuple(projection.get("pending_interactions", ())),
                )
            else:
                event = StreamEvent(event="run.progress", data=projection)
            yield event.model_copy(
                update={
                    "id": f"{run_id}:{revision}",
                    "data": {
                        **event.data,
                        "revision": revision,
                        "snapshot": True,
                        **({"reset_reason": reset} if reset else {}),
                    },
                }
            )
        cursor, first, reset = revision, False, None
        if status in {"completed", "failed", "cancelled", "interrupted", "cancellation_requested"}:
            return
        await asyncio.sleep(admission.settings.coordinator_poll_seconds)
