"""Normalized trusted channel input and immediate response port."""

from dataclasses import dataclass
from typing import Protocol


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
