"""飞书官方 Channel SDK 适配器：WebSocket 生命周期、事件规范化与消息发送。"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from threading import Lock
from typing import Any

from financeclaw.application.feishu_channel_service import (
    FeishuChannelService,
    FeishuInboundMessage,
    FeishuMarkdownStream,
    MarkdownProducer,
)

LOGGER = logging.getLogger(__name__)


class FeishuChannelAdapter:
    """把 ``lark-channel-sdk`` 适配到 FinanceClaw 应用层 Port。

    SDK 只在 ``start`` 时延迟导入；飞书回调所在 SDK 后台事件循环只负责
    规范化和投递，实际 Conversation/Agent 调用被桥接回 FastAPI 主事件循环。
    """

    def __init__(
        self,
        service: FeishuChannelService,
        *,
        app_id: str,
        app_secret: str,
        allowed_open_ids: frozenset[str],
        max_concurrency: int,
        security_mode: str = "audit",
        connect_timeout_seconds: float = 30.0,
        tenant_cache_size: int = 10_000,
    ) -> None:
        """保存飞书配置，但不导入 SDK 或建立网络连接。

        Args:
            service: 飞书 P2P 应用服务。
            app_id: 飞书应用 ID。
            app_secret: 飞书应用 Secret，仅传给 SDK，不写日志。
            allowed_open_ids: 灰度用户白名单。
            max_concurrency: SDK WebSocket 回调并发安全上限。
            security_mode: SDK security mode（audit 或 strict）。
            connect_timeout_seconds: 启动等待 WebSocket 就绪的超时。
            tenant_cache_size: 原始事件 tenant_key 关联缓存上限。

        """
        if not app_id.strip() or not app_secret or not allowed_open_ids:
            raise ValueError("Feishu app_id, app_secret and allowlist are required")
        if max_concurrency < 1:
            raise ValueError("Feishu max_concurrency must be positive")
        if security_mode not in {"audit", "strict"}:
            raise ValueError("Feishu security_mode must be audit or strict")
        if connect_timeout_seconds <= 0 or tenant_cache_size < 1:
            raise ValueError("Feishu timeout and tenant cache size must be positive")
        self.service = service
        self.app_id = app_id
        self._app_secret = app_secret
        self.allowed_open_ids = allowed_open_ids
        self.max_concurrency = max_concurrency
        self.security_mode = security_mode
        self.connect_timeout_seconds = connect_timeout_seconds
        self.tenant_cache_size = tenant_cache_size
        self._channel: Any | None = None
        self._application_loop: asyncio.AbstractEventLoop | None = None
        self._tenant_by_message: OrderedDict[str, str] = OrderedDict()
        self._tenant_lock = Lock()
        self._ready = False
        self._accepting_messages = False

    async def start(self) -> None:
        """创建官方 SDK Channel，注册回调并等待 WebSocket 就绪。"""
        if self._ready:
            return
        self._application_loop = asyncio.get_running_loop()
        channel = self._build_channel()
        channel.on("raw", self._capture_raw_event)
        channel.on("message", self._on_message)
        channel.on("error", self._on_error)
        channel.on("reconnecting", self._on_reconnecting)
        channel.on("reconnected", self._on_reconnected)
        self._channel = channel
        try:
            await channel.connect_until_ready(timeout=self.connect_timeout_seconds)
        except Exception:
            with suppress(Exception):
                await channel.disconnect()
            self._channel = None
            self._application_loop = None
            self._accepting_messages = False
            raise
        self._ready = True
        self._accepting_messages = True
        LOGGER.info("Feishu channel connected", extra={"app_id": self.app_id})

    async def stop(self) -> None:
        """先排空已受理业务任务，再断开飞书 WebSocket。"""
        self._accepting_messages = False
        try:
            await self.service.shutdown()
        finally:
            channel = self._channel
            self._ready = False
            try:
                if channel is not None:
                    await channel.disconnect()
            finally:
                self._channel = None
                self._application_loop = None
                LOGGER.info("Feishu channel stopped", extra={"app_id": self.app_id})

    async def health(self) -> bool:
        """返回飞书 Channel 是否已就绪且未处于连接错误状态。"""
        channel = self._channel
        if not self._ready or channel is None or not bool(getattr(channel, "is_ready", False)):
            return False
        snapshot = getattr(channel, "connection_snapshot", lambda: None)()
        return snapshot is None or getattr(snapshot, "state", "connected") != "error"

    async def stream_markdown(
        self,
        *,
        chat_id: str,
        reply_to_message_id: str,
        producer: MarkdownProducer,
    ) -> bool:
        """调用 SDK 高层 CardKit Markdown 流，并返回发送成功标志。"""
        channel = self._require_channel()

        async def sdk_producer(stream: FeishuMarkdownStream) -> None:
            """把 SDK 控制器以应用层最小协议交给生产者。"""
            await producer(stream)

        result = await channel.stream(
            chat_id,
            {"markdown": sdk_producer},
            {"reply_to": reply_to_message_id},
        )
        return bool(getattr(result, "success", False))

    async def send_text(
        self,
        *,
        chat_id: str,
        reply_to_message_id: str,
        text: str,
        idempotency_key: str,
    ) -> bool:
        """通过 SDK 发送带 UUID 幂等键的普通文本回复。"""
        channel = self._require_channel()
        result = await channel.send(
            chat_id,
            {"text": text},
            {"reply_to": reply_to_message_id, "uuid": idempotency_key},
        )
        return bool(getattr(result, "success", False))

    def normalize_message(self, message: Any) -> FeishuInboundMessage:
        """把 SDK ``InboundMessage`` 转为不携带原始事件的应用消息。

        Args:
            message: 官方 SDK 规范化消息对象。

        Returns:
            仅含一期业务必需字段的不可变入站消息。

        """
        message_id = str(getattr(message, "message_id", getattr(message, "id", "")) or "")
        sender = getattr(message, "sender", None)
        conversation = getattr(message, "conversation", None)
        tenant_key = str(getattr(sender, "tenant_key", "") or "")
        if not tenant_key:
            with self._tenant_lock:
                tenant_key = self._tenant_by_message.pop(message_id, "")
        body_text = getattr(message, "body_text", None)
        if not isinstance(body_text, str):
            body_text = str(getattr(message, "content_text", "") or "")
        return FeishuInboundMessage(
            message_id=message_id,
            tenant_key=tenant_key,
            sender_open_id=str(getattr(sender, "open_id", getattr(message, "sender_id", "")) or ""),
            chat_id=str(getattr(conversation, "chat_id", getattr(message, "chat_id", "")) or ""),
            chat_type=str(
                getattr(
                    conversation,
                    "chat_type",
                    getattr(message, "chat_type", "unknown"),
                )
                or "unknown"
            ),
            content_type=str(getattr(message, "raw_content_type", "") or ""),
            text=body_text,
            sender_type=getattr(sender, "sender_type", getattr(message, "sender_type", None)),
            sender_is_bot=bool(getattr(sender, "is_bot", getattr(message, "sender_is_bot", False))),
        )

    def _build_channel(self) -> Any:
        """延迟导入并构造官方飞书 SDK Channel。"""
        try:
            from lark_channel import (
                ChatQueueConfig,
                FeishuChannel,
                InboundConfig,
                PolicyConfig,
                SafetyConfig,
                SecurityConfig,
                TextBatchConfig,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Feishu channel is enabled but lark-channel-sdk is not installed"
            ) from exc
        return FeishuChannel(
            app_id=self.app_id,
            app_secret=self._app_secret,
            policy=PolicyConfig(
                dm_policy="allowlist",
                group_policy="disabled",
                allow_from=sorted(self.allowed_open_ids),
            ),
            safety=SafetyConfig(
                text_batch=TextBatchConfig(
                    delay_ms=0,
                    long_delay_ms=0,
                    max_messages=1,
                ),
                chat_queue=ChatQueueConfig(enabled=False, merge_while_busy=False),
            ),
            inbound=InboundConfig(
                expand_merge_forward=False,
                fetch_interactive_card=False,
                reaction_notifications="off",
                include_raw=False,
                emit_raw_events=True,
            ),
            security=SecurityConfig(
                mode=self.security_mode,
                strict_content_text=self.security_mode == "strict",
                max_concurrent_ws_handlers=self.max_concurrency,
            ),
        )

    def _capture_raw_event(self, payload: Any) -> None:
        """只从已验证事件提取 message_id→tenant_key，绝不保存正文或原文。"""
        if not isinstance(payload, Mapping):
            return
        envelope = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        header = envelope.get("header") if isinstance(envelope.get("header"), Mapping) else {}
        event = envelope.get("event") if isinstance(envelope.get("event"), Mapping) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), Mapping) else {}
        message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
        message_id = str(message.get("message_id") or "")
        tenant_key = str(header.get("tenant_key") or sender.get("tenant_key") or "")
        if not message_id or not tenant_key:
            return
        with self._tenant_lock:
            self._tenant_by_message[message_id] = tenant_key
            self._tenant_by_message.move_to_end(message_id)
            while len(self._tenant_by_message) > self.tenant_cache_size:
                self._tenant_by_message.popitem(last=False)

    def _on_message(self, message: Any) -> None:
        """在 SDK 回调线程规范化消息，并无阻塞地投递到应用主循环。"""
        if not self._accepting_messages:
            return
        try:
            inbound = self.normalize_message(message)
        except Exception:
            LOGGER.warning("Feishu inbound normalization failed")
            return
        loop = self._application_loop
        if loop is None or loop.is_closed():
            LOGGER.warning("Feishu inbound message arrived before application loop was ready")
            return
        loop.call_soon_threadsafe(self._submit_on_application_loop, inbound)

    def _submit_on_application_loop(self, inbound: FeishuInboundMessage) -> None:
        """在 FastAPI 主循环创建受并发约束的业务任务。"""
        self.service.submit(inbound, self)

    def _on_error(self, error: Any) -> None:
        """记录脱敏 SDK 错误分类，不输出凭证、原始事件或正文。"""
        LOGGER.warning(
            "Feishu SDK reported an error",
            extra={
                "error_type": type(error).__name__,
                "error_code": str(getattr(error, "code", "unknown")),
            },
        )

    def _on_reconnecting(self) -> None:
        """记录飞书 WebSocket 正在重连。"""
        LOGGER.warning("Feishu channel reconnecting", extra={"app_id": self.app_id})

    def _on_reconnected(self) -> None:
        """记录飞书 WebSocket 已恢复。"""
        LOGGER.info("Feishu channel reconnected", extra={"app_id": self.app_id})

    def _require_channel(self) -> Any:
        """返回已启动 SDK Channel，未启动时抛出明确错误。"""
        if self._channel is None or not self._ready:
            raise RuntimeError("Feishu channel is not connected")
        return self._channel
