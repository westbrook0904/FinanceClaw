"""飞书 P2P Channel 应用服务：身份映射、会话复用、串行执行与回复编排。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from financeclaw.bff.application.conversation_service import ConversationService
from financeclaw.bff.application.feishu_interactions import (
    accepts_text_reply,
    format_interactions,
    parse_response,
)
from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.kernel.notifications import NotificationAddress
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.conversation.repository import ConversationConflict, ConversationNotFound
from financeclaw.shared.execution_ledger.interactions import (
    InteractionConflict,
    InteractionNotFound,
)
from financeclaw.shared.execution_ledger.repository import digest

LOGGER = logging.getLogger(__name__)


class FeishuReplyGateway(Protocol):
    """普通入站校验反馈；任务交付全部使用持久通知。"""

    async def send_text(
        self, *, chat_id: str, reply_to_message_id: str, text: str, idempotency_key: str
    ) -> bool:
        """发送带固定 UUID 的简短文本反馈。"""
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


class FeishuChannelService:
    """飞书 P2P 消息到 Conversation Turn 的一期编排服务。

    同一个 chat 通过内存锁串行，不同 chat 受全局信号量限制并行；消息 ID
    同时进入持久化 Turn 幂等键，SDK 重推不会重复执行或追加 Journal。
    内存锁只覆盖本服务实例，持久化幂等负责重放保护，不能据此推导出多实例
    Channel 的全局串行保证。任务卡展示持久状态，最终正文以 Journal 为准。
    """

    UNSUPPORTED_TEXT = "当前仅支持文本消息。"
    EMPTY_TEXT = "消息内容不能为空。"
    FAILED_TEXT = "处理失败，请稍后重试。"

    def __init__(
        self,
        conversation_service: ConversationService,
        *,
        app_id: str,
        allowed_open_ids: frozenset[str],
        scopes: frozenset[str],
        max_concurrency: int = 8,
    ) -> None:
        """装配飞书 Channel 服务并校验权限与并发参数。

        Args:
            conversation_service: 复用现有 Journal 与 Agent Server 的会话服务。
            app_id: 飞书应用 ID。
            allowed_open_ids: 允许使用的用户 open_id 列表。
            scopes: 显式授予飞书身份的 FinanceClaw scopes。
            max_concurrency: 不同单聊同时执行的最大数量。

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
        self.conversation_service = conversation_service
        self.app_id = app_id
        self.allowed_open_ids = allowed_open_ids
        self.scopes = scopes
        from financeclaw.bff.application.feishu_card_actions import FeishuCardActions

        self.card_actions = FeishuCardActions(
            conversation_service.runs,
            app_id=app_id,
            allowed_open_ids=allowed_open_ids,
            scopes=scopes,
        )
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
                except (InteractionConflict, InteractionNotFound) as exc:
                    await self._send_plain(
                        gateway,
                        message,
                        "这条回答暂未被接收：问题可能已过期、已处理，或回答格式不符合要求。"
                        "请以最新问题为准，在原单聊中回复。",
                        suffix="interaction-conflict",
                    )
                    LOGGER.info(
                        "Feishu interaction rejected", extra={"error_type": type(exc).__name__}
                    )
                    return "interaction_conflict"
                except ConversationConflict:
                    if message.text.strip().split(maxsplit=1)[0] in {
                        "/answer",
                        "/choose",
                        "/approve",
                        "/reject",
                        "/cancel",
                    }:
                        await self._send_plain(
                            gateway,
                            message,
                            "交互未恢复：事件身份与已绑定单聊不一致，请从原问题所在会话操作。",
                            suffix="interaction-identity",
                        )
                        return "interaction_conflict"
                    await self._send_plain(
                        gateway,
                        message,
                        "上一条任务仍在处理中，请等待后续回复。",
                        suffix="active-turn",
                    )
                    return "waiting_active_turn"
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
        """解析会话绑定、幂等开启 Turn，并登记持久化任务卡。"""
        tenant_id = f"feishu:{message.tenant_key}"
        subject_id = f"feishu:{message.sender_open_id}"
        current = datetime.now(UTC)
        authorization_kwargs = {
            "authorization": AuthorizationEvidence(
                source="feishu",
                source_hash=digest(
                    [
                        self.app_id,
                        message.tenant_key,
                        message.sender_open_id,
                        message.chat_id,
                        message.message_id,
                    ]
                ),
                issued_at=current,
                expires_at=current
                + timedelta(seconds=self.conversation_service.runs.settings.bff_run_grant_seconds),
            )
        }
        conversation = await self.conversation_service.get_or_create_channel_conversation(
            channel="feishu",
            app_id=self.app_id,
            tenant_key=message.tenant_key,
            external_user_id=message.sender_open_id,
            external_chat_id=message.chat_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        parsed = parse_response(normalized)
        channel_state = {}
        command = normalized.split(maxsplit=1)[0]
        if parsed is None and not normalized.startswith("/"):
            channel_state = await asyncio.to_thread(
                self.conversation_service.runs.interactions.channel_state,
                conversation.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                response_key=f"feishu:{self.app_id}:{message.message_id}",
            )
            items = channel_state.get("interactions", [])
            previous = channel_state.get("answered")
            item = previous or (items[0] if len(items) == 1 else None)
            if (
                item
                and accepts_text_reply(item)
                and (previous or item["status"] == "pending")
                and channel_state.get("waiting_reason") != "authorization_required"
            ):
                try:
                    parsed = (
                        item["interaction_id"],
                        InteractionResponse(
                            revision=item["revision"], kind="input", answer={"text": normalized}
                        ),
                    )
                except ValueError as exc:
                    raise InteractionConflict("text answer exceeds the response limit") from exc
            elif previous:
                raise InteractionConflict("message was already used for another response")
            elif channel_state.get("root_run_id"):
                waiting_reason = channel_state.get("waiting_reason")
                fallback = (
                    "这次提问已过期。请结束当前任务，再重新发起请求。"
                    if waiting_reason == "interaction_expired"
                    else "当前任务需要重新授权，请按上一条授权提示操作。"
                    if waiting_reason == "authorization_required"
                    else "上一条任务仍在处理中，请稍候。"
                )
                fallback += f"\n\n如需结束任务，请发送：\n/cancel {channel_state['root_run_id']}"
                await self._send_plain(
                    gateway,
                    message,
                    fallback
                    if waiting_reason == "authorization_required"
                    else format_interactions(items, fallback=fallback),
                    suffix="pending-task",
                )
                return "waiting_active_turn"
        if parsed is not None:
            identifier, response = parsed
            await self.conversation_service.runs.interactions.respond(
                identifier,
                response,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=self.scopes,
                conversation_id=conversation.conversation_id,
                idempotency_key=f"feishu:{self.app_id}:{message.message_id}",
                **authorization_kwargs,
            )
            return "accepted"
        if command in {"/cancel", "/authorize", "/revoke", "/mute"}:
            parts = normalized.split()
            if len(parts) != 2:
                raise InteractionConflict("命令必须携带一个明确的根任务 ID。")
            try:
                turn = await asyncio.to_thread(
                    self.conversation_service.repository.get_turn_owned,
                    parts[1],
                    tenant_id,
                    subject_id,
                )
            except ConversationNotFound as exc:
                raise InteractionNotFound("root task not found") from exc
            if turn.conversation_id != conversation.conversation_id:
                raise InteractionConflict("任务不属于当前单聊。")
            if parts[0] == "/mute":
                await asyncio.to_thread(
                    self.conversation_service.runs.notifications,
                    parts[1],
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    revoke=True,
                )
                await self._send_plain(gateway, message, "已关闭该任务的后续通知。", suffix="mute")
                return "notifications_muted"
            if parts[0] in {"/authorize", "/revoke"}:
                if parts[0] == "/authorize":
                    result = await self.conversation_service.runs.reauthorize(
                        parts[1],
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        scopes=self.scopes,
                        **authorization_kwargs,
                        command_id=f"feishu:{self.app_id}:{message.message_id}",
                    )
                else:
                    result = await self.conversation_service.runs.revoke_authorization(
                        parts[1],
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        command_id=f"feishu:{self.app_id}:{message.message_id}",
                    )
                await self._send_plain(
                    gateway,
                    message,
                    "已更新任务的有限后台授权。"
                    if parts[0] == "/authorize"
                    else "已撤销任务的后台授权。",
                    suffix=parts[0][1:],
                )
                return result.status
            result = await self.conversation_service.cancel(
                parts[1], tenant_id=tenant_id, subject_id=subject_id
            )
            await self._send_plain(
                gateway,
                message,
                "任务已确认停止；已发生的外部操作不会自动回滚。"
                if result.status == "cancelled"
                else "已请求取消，仍在确认执行停止；暂时不能复用该任务。"
                if result.status == "cancellation_requested"
                else f"任务已经结束（{result.status}），没有新增取消操作。",
                suffix="cancel",
            )
            return result.status
        await self.conversation_service.start_turn(
            conversation.conversation_id,
            ConversationTurnRequest(message=normalized),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=self.scopes,
            idempotency_key=f"feishu:{self.app_id}:{message.message_id}",
            notification_address=NotificationAddress(
                app_id=self.app_id,
                tenant_key=message.tenant_key,
                open_id=message.sender_open_id,
                chat_id=message.chat_id,
                message_id=message.message_id,
            ),
            **authorization_kwargs,
        )
        return "accepted"

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
