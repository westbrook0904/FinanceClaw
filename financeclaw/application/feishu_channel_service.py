"""飞书 P2P Channel 应用服务：身份映射、会话复用、串行执行与回复编排。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from financeclaw.kernel import ConversationTurnRequest, StreamEvent

from .conversation_service import ConversationService

LOGGER = logging.getLogger(__name__)


class FeishuMarkdownStream(Protocol):
    """飞书 Markdown 流控制器的最小应用层契约。"""

    async def append(self, chunk: str) -> None:
        """追加一段助手文本。"""
        ...

    async def set_content(self, full: str) -> None:
        """用完整文本覆盖当前卡片内容。"""
        ...


MarkdownProducer = Callable[[FeishuMarkdownStream], Awaitable[None]]


class FeishuReplyGateway(Protocol):
    """应用服务发送飞书流式卡片与降级文本所依赖的最小 Port。"""

    async def stream_markdown(
        self,
        *,
        chat_id: str,
        reply_to_message_id: str,
        producer: MarkdownProducer,
    ) -> bool:
        """发送回复原消息的 Markdown 流式卡片，成功时返回 True。"""
        ...

    async def send_text(
        self,
        *,
        chat_id: str,
        reply_to_message_id: str,
        text: str,
        idempotency_key: str,
    ) -> bool:
        """发送回复原消息的普通文本，成功时返回 True。"""
        ...


@dataclass(frozen=True, slots=True)
class FeishuInboundMessage:
    """由 SDK 适配器从已验证飞书事件生成的最小入站消息。

    Attributes:
        message_id: 飞书消息 ID，也是 Turn 幂等键的稳定来源。
        tenant_key: 飞书租户键，只能来自验证后的事件信封。
        sender_open_id: 发件人 open_id。
        chat_id: P2P 单聊 ID。
        chat_type: 飞书聊天类型。
        content_type: 飞书原始消息类型，一期仅接受 text。
        text: SDK 规范化后的文本正文。
        sender_type: SDK 识别的发件人类型。
        sender_is_bot: SDK 的机器人身份判断。

    """

    message_id: str
    tenant_key: str
    sender_open_id: str
    chat_id: str
    chat_type: str
    content_type: str
    text: str
    sender_type: str | None = None
    sender_is_bot: bool = False


@dataclass(slots=True)
class _ReplyState:
    """单次飞书回复在流式展示与最终校正之间共享的可变状态。"""

    live_text: str = ""
    final_text: str | None = None
    terminal_status: str | None = None
    showing_progress: bool = False


class FeishuChannelService:
    """飞书 P2P 消息到 Conversation Turn 的一期编排服务。

    同一个 chat 通过内存锁串行，不同 chat 受全局信号量限制并行；消息 ID
    同时进入持久化 Turn 幂等键，SDK 重推不会重复执行或追加 Journal。
    """

    UNSUPPORTED_TEXT = "当前仅支持文本消息。"
    EMPTY_TEXT = "消息内容不能为空。"
    PROGRESS_TEXT = "正在处理…"
    INTERRUPTED_TEXT = "该请求需要人工审批，请前往 Web/API 完成审批。"
    FAILED_TEXT = "处理失败，请稍后重试。"
    STILL_RUNNING_TEXT = "请求仍在处理中，请稍后通过 Web/API 查看结果。"

    def __init__(
        self,
        conversation_service: ConversationService,
        *,
        app_id: str,
        allowed_open_ids: frozenset[str],
        scopes: frozenset[str],
        max_concurrency: int = 8,
        status_poll_interval_seconds: float = 0.25,
        status_timeout_seconds: float = 300.0,
    ) -> None:
        """装配飞书 Channel 服务并校验并发与轮询参数。

        Args:
            conversation_service: 复用现有 Journal 与 Agent Server 的会话服务。
            app_id: 飞书应用 ID。
            allowed_open_ids: 灰度用户 open_id 白名单。
            scopes: 显式授予飞书身份的 FinanceClaw scopes。
            max_concurrency: 不同单聊同时执行的最大数量。
            status_poll_interval_seconds: delegation 等场景的状态轮询间隔。
            status_timeout_seconds: 流结束后等待最终状态的最长秒数。

        Raises:
            ValueError: 必填值为空或数值范围非法。

        """
        if (
            not app_id.strip()
            or not allowed_open_ids
            or any(not item.strip() for item in allowed_open_ids)
            or not scopes
            or any(not item.strip() for item in scopes)
        ):
            raise ValueError("Feishu app_id, allowlist and scopes are required")
        if max_concurrency < 1:
            raise ValueError("Feishu max_concurrency must be positive")
        if status_poll_interval_seconds < 0 or status_timeout_seconds <= 0:
            raise ValueError("Feishu status polling values are invalid")
        self.conversation_service = conversation_service
        self.app_id = app_id
        self.allowed_open_ids = allowed_open_ids
        self.scopes = scopes
        self.status_poll_interval_seconds = status_poll_interval_seconds
        self.status_timeout_seconds = status_timeout_seconds
        self._concurrency = asyncio.Semaphore(max_concurrency)
        self._chat_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[str]] = set()

    def submit(
        self,
        message: FeishuInboundMessage,
        gateway: FeishuReplyGateway,
    ) -> asyncio.Task[str] | None:
        """把已通过基础准入的消息提交为后台任务并立即返回。

        Args:
            message: SDK 适配后的入站消息。
            gateway: 飞书回复 Port。

        Returns:
            已创建的任务；应静默忽略的消息返回 ``None``。

        """
        if self._should_ignore(message):
            return None
        task = asyncio.create_task(
            self.process(message, gateway),
            name=f"feishu-turn-{message.message_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def process(self, message: FeishuInboundMessage, gateway: FeishuReplyGateway) -> str:
        """处理一条飞书消息，供后台投递与自动化测试共同调用。

        Args:
            message: SDK 适配后的入站消息。
            gateway: 飞书回复 Port。

        Returns:
            ``ignored``、``unsupported``、``empty`` 或运行的最终公开状态。

        """
        if self._should_ignore(message):
            return "ignored"
        lock_key = (message.tenant_key, message.chat_id)
        lock = self._chat_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            async with self._concurrency:
                try:
                    if message.content_type != "text":
                        await self._send_plain(
                            gateway,
                            message,
                            self.UNSUPPORTED_TEXT,
                            suffix="unsupported",
                        )
                        return "unsupported"
                    normalized = message.text.strip()
                    if not normalized:
                        await self._send_plain(gateway, message, self.EMPTY_TEXT, suffix="empty")
                        return "empty"
                    return await self._process_text(message, normalized, gateway)
                except Exception as exc:
                    LOGGER.warning(
                        "Feishu message processing failed",
                        extra={
                            "message_id": message.message_id,
                            "chat_id": message.chat_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    await self._send_plain(
                        gateway,
                        message,
                        self.FAILED_TEXT,
                        suffix="processing-failed",
                        suppress_error=True,
                    )
                    return "failed"

    async def shutdown(self) -> None:
        """等待已受理的进程内消息任务结束；取消由外层关闭超时负责。"""
        pending = tuple(task for task in self._tasks if not task.done())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _task_done(self, task: asyncio.Task[str]) -> None:
        """从在途集合移除已结束任务并消费异常，避免悬空任务告警。"""
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _should_ignore(self, message: FeishuInboundMessage) -> bool:
        """判断消息是否因来源、聊天类型或身份不可信而静默忽略。"""
        if (
            not message.message_id
            or not message.tenant_key
            or not message.sender_open_id
            or not message.chat_id
        ):
            return True
        if message.chat_type != "p2p" or message.sender_open_id not in self.allowed_open_ids:
            return True
        if message.sender_is_bot:
            return True
        return message.sender_type is not None and message.sender_type != "user"

    async def _process_text(
        self,
        message: FeishuInboundMessage,
        normalized: str,
        gateway: FeishuReplyGateway,
    ) -> str:
        """解析会话绑定、幂等开启 Turn，并交付流式回复。"""
        tenant_id = f"feishu:{message.tenant_key}"
        subject_id = f"feishu:{message.sender_open_id}"
        conversation = await self.conversation_service.get_or_create_channel_conversation(
            channel="feishu",
            app_id=self.app_id,
            tenant_key=message.tenant_key,
            external_user_id=message.sender_open_id,
            external_chat_id=message.chat_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        accepted = await self.conversation_service.start_turn(
            conversation.conversation_id,
            ConversationTurnRequest(message=normalized),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=self.scopes,
            idempotency_key=f"feishu:{self.app_id}:{message.message_id}",
        )
        state = _ReplyState()

        async def producer(stream: FeishuMarkdownStream) -> None:
            """把稳定应用事件写入 SDK 流控制器并最终以 Journal 校正。"""
            try:
                async for event in self.conversation_service.stream(
                    accepted.run_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    scopes=self.scopes,
                ):
                    await self._apply_event(stream, event, state)
            except Exception:
                LOGGER.warning(
                    "Feishu application stream interrupted",
                    extra={"run_id": accepted.run_id, "message_id": message.message_id},
                )
            if state.terminal_status is None:
                await self._resolve_final(
                    stream,
                    state,
                    run_id=accepted.run_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )

        try:
            delivered = await gateway.stream_markdown(
                chat_id=message.chat_id,
                reply_to_message_id=message.message_id,
                producer=producer,
            )
            if not delivered:
                raise RuntimeError("Feishu streaming card delivery returned failure")
        except Exception:
            LOGGER.warning(
                "Feishu streaming card failed; falling back to text",
                extra={"run_id": accepted.run_id, "message_id": message.message_id},
            )
            if state.terminal_status is None:
                await self._resolve_final(
                    None,
                    state,
                    run_id=accepted.run_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )
            await self._send_plain(
                gateway,
                message,
                state.final_text or self.FAILED_TEXT,
                suffix="fallback",
            )
        return state.terminal_status or "completed"

    async def _apply_event(
        self,
        stream: FeishuMarkdownStream,
        event: StreamEvent,
        state: _ReplyState,
    ) -> None:
        """把一个稳定应用事件投影到 Markdown 卡片。"""
        data = event.data if isinstance(event.data, Mapping) else {}
        if event.event == "assistant.delta":
            delta = data.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            if state.showing_progress and not state.live_text:
                state.live_text = delta
                state.showing_progress = False
                await stream.set_content(delta)
            else:
                state.live_text += delta
                await stream.append(delta)
            return
        if event.event == "run.progress":
            if not state.live_text and not state.showing_progress:
                state.showing_progress = True
                await stream.set_content(self.PROGRESS_TEXT)
            return
        if event.event == "assistant.completed":
            content = data.get("content")
            if isinstance(content, str) and content:
                state.final_text = content
            state.terminal_status = "completed"
            await stream.set_content(state.final_text or state.live_text or "处理已完成。")
            return
        if event.event == "run.interrupted":
            state.terminal_status = "interrupted"
            state.final_text = self.INTERRUPTED_TEXT
            await stream.set_content(state.final_text)
            return
        if event.event == "run.failed":
            state.terminal_status = "failed"
            state.final_text = self.FAILED_TEXT
            await stream.set_content(state.final_text)

    async def _resolve_final(
        self,
        stream: FeishuMarkdownStream | None,
        state: _ReplyState,
        *,
        run_id: str,
        tenant_id: str,
        subject_id: str,
    ) -> None:
        """轮询权威状态，推进 delegation，并用 Journal 的最终文本校正展示。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.status_timeout_seconds
        while True:
            response = await self.conversation_service.status(
                run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=self.scopes,
            )
            if response.status == "completed":
                state.terminal_status = "completed"
                state.final_text = await self.conversation_service.assistant_content(
                    run_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                )
                state.final_text = state.final_text or state.live_text or "处理已完成。"
                if stream is not None:
                    await stream.set_content(state.final_text)
                return
            if response.status == "interrupted":
                state.terminal_status = "interrupted"
                state.final_text = self.INTERRUPTED_TEXT
                if stream is not None:
                    await stream.set_content(state.final_text)
                return
            if response.status == "failed":
                state.terminal_status = "failed"
                state.final_text = self.FAILED_TEXT
                if stream is not None:
                    await stream.set_content(state.final_text)
                return
            if stream is not None and not state.live_text and not state.showing_progress:
                state.showing_progress = True
                await stream.set_content(self.PROGRESS_TEXT)
            if loop.time() >= deadline:
                state.terminal_status = "running"
                state.final_text = self.STILL_RUNNING_TEXT
                if stream is not None:
                    await stream.set_content(state.final_text)
                return
            await asyncio.sleep(self.status_poll_interval_seconds)

    async def _send_plain(
        self,
        gateway: FeishuReplyGateway,
        message: FeishuInboundMessage,
        text: str,
        *,
        suffix: str,
        suppress_error: bool = False,
    ) -> None:
        """发送带稳定 UUID 的普通文本回复，可选吞掉二次降级失败。"""
        try:
            delivered = await gateway.send_text(
                chat_id=message.chat_id,
                reply_to_message_id=message.message_id,
                text=text,
                idempotency_key=str(
                    uuid5(NAMESPACE_URL, f"financeclaw:{self.app_id}:{message.message_id}:{suffix}")
                ),
            )
            if not delivered:
                raise RuntimeError("Feishu text delivery returned failure")
        except Exception:
            if not suppress_error:
                raise
            LOGGER.warning(
                "Feishu text fallback failed",
                extra={"message_id": message.message_id, "chat_id": message.chat_id},
            )
