"""原生正文流的有界展示投影；执行结果和最终交付仍由 checkpoint 与 Journal 确认。"""

import asyncio
from contextlib import aclosing
from time import monotonic

from sqlalchemy import select

from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.notifications.facts import target_valid
from financeclaw.shared.notifications.tables import NotificationEventRow, NotificationTargetRow
from financeclaw.shared.turns.tables import TurnCommandRow
from financeclaw.shared.turns.types import digest

PREVIEW_BYTES = 12000


def consume(state, part):
    """只合并根 model 的公开文本；工具、子图、思考块和其他节点均不能成为预览。"""
    if not part.id or part.id == state.get("cursor"):
        return
    state["cursor"] = part.id
    if part.event != "messages" or not isinstance(part.data, (list, tuple)):
        return
    message, metadata = part.data
    namespace = metadata.get("langgraph_checkpoint_ns", "")
    if (
        metadata.get("langgraph_node") != "model"
        or not namespace.startswith("model:")
        or "|" in namespace
        or message.get("type") not in {"ai", "AIMessageChunk"}
        or not message.get("id")
    ):
        return
    if message["id"] != state.get("message_id"):
        state.update(message_id=message["id"], text="", blocked=False, truncated=False)
    if any(message.get(key) for key in ("tool_calls", "invalid_tool_calls", "tool_call_chunks")):
        state.update(text="", blocked=True, truncated=False)
    if state.get("blocked"):
        return
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    if not isinstance(content, str):
        return
    text = (state.get("text", "") if message["type"] == "AIMessageChunk" else "") + content
    state["truncated"] = state.get("truncated", False) or len(text.encode()) > PREVIEW_BYTES
    state["text"] = text.encode()[:PREVIEW_BYTES].decode("utf-8", errors="ignore")


class TurnAnswerStream:
    """借用现有 join 槽位，按秒合并卡片快照，并原子保存文本与重连游标。"""

    def __init__(self, service, native, *, interval=1.0):
        """注入现有业务存储与 SDK，不创建第二套执行或投递队列。"""
        self.service, self.native, self.interval = service, native, interval

    def _target(self, session, turn, command):
        """和状态迁移共用根锁，停止、换轮、静音与绑定失效立即封住旧流。"""
        root = self.service.store.lock(session, turn["turn_id"])
        current = session.get(TurnCommandRow, root.current_command_id)
        if (
            root.current_command_id != command["command_id"]
            or current.native_run_id != command["native_run_id"]
            or root.status not in {"queued", "running", "resuming"}
            or root.cancel_requested_at
        ):
            return root, None
        target = session.scalar(
            select(NotificationTargetRow).where(NotificationTargetRow.turn_id == root.turn_id)
        )
        if target is None or not target_valid(session, target):
            return root, None
        return root, target

    def read(self, turn, command):
        """取当前命令的持久化游标；新命令不能继承上一段正文。"""
        with self.service.sessions.begin() as session:
            _, target = self._target(session, turn, command)
            if target is None:
                return None
            state = target.card_payload.get("stream", {})
            if state.get("command_id") != command["command_id"]:
                return {"command_id": command["command_id"], "text": ""}
            return dict(state)

    def save(self, turn, command, state, *, expected_cursor):
        """游标 CAS 拒绝并发订阅者覆盖；展示序号独立于业务 revision 和审计。"""
        with self.service.sessions.begin() as session:
            root, target = self._target(session, turn, command)
            if target is None:
                return False
            previous = target.card_payload.get("stream", {})
            if previous.get("command_id") != command["command_id"]:
                previous = {}
            if previous.get("cursor") != expected_cursor:
                return False
            payload = {**target.card_payload, "stream": dict(state), "revision": root.revision}
            if (previous.get("text", ""), previous.get("truncated", False)) != (
                state.get("text", ""),
                state.get("truncated", False),
            ):
                sequence = (
                    max(root.revision, payload.get("card_revision", target.card_sequence)) + 1
                )
                payload["card_revision"] = sequence
                session.add(
                    NotificationEventRow(
                        event_id=digest([target.target_id, "preview", sequence]),
                        target_id=target.target_id,
                        revision=sequence,
                        kind="card",
                        payload=payload,
                    )
                )
            target.card_payload = payload
            return True

    async def join(self, turn, command):
        """及时发布首段，后续定时合并；断连仅结束观察，剩余文本由原生游标重放。"""
        state = await run_sync(self.read, turn, command)
        if state is None:
            await self.native.join(turn, command)
            return
        cursor = state.get("cursor")
        next_flush = monotonic()
        stream = self.native.stream(turn, command, last_event_id=cursor or "-1")
        async with aclosing(stream):
            pending = asyncio.create_task(anext(stream, None))
            try:
                while True:
                    done, _ = await asyncio.wait(
                        [pending], timeout=max(0, next_flush - monotonic())
                    )
                    ended = False
                    if done:
                        part = pending.result()
                        ended = part is None
                        if part is not None:
                            consume(state, part)
                            pending = asyncio.create_task(anext(stream, None))
                    if ended or monotonic() >= next_flush:
                        if state.get("cursor") != cursor:
                            if not await run_sync(
                                self.save, turn, command, state, expected_cursor=cursor
                            ):
                                return
                            cursor = state.get("cursor")
                        next_flush = monotonic() + self.interval
                    if ended:
                        return
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
