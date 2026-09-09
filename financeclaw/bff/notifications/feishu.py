"""飞书官方 SDK 底层接口适配：只回复原消息，不使用高层重试、分片或降级。"""

import json
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Receipt:
    """明确回执与未知结果分开；仅可信消息 ID 才能记为 sent。"""

    status: Literal["sent", "retry", "failed", "uncertain", "suppressed"]
    message_id: str | None = None
    error_class: str | None = None


class FeishuNotificationGateway:
    """无 WebSocket 的独立 SDK 客户端；既有 Channel 布尔 gateway 不用作回执。"""

    def __init__(self, client):
        """注入官方 Client，测试使用其真实请求模型与传输协议。"""
        self.client = client

    @classmethod
    def from_settings(cls, settings):
        """只有显式启动的发送进程才装配凭证和 SDK。"""
        from lark_channel import Client, LogLevel

        client = (
            Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret.get_secret_value())
            .timeout(settings.notification_timeout_seconds)
            .log_level(LogLevel.ERROR)
            .build()
        )
        return cls(client)

    async def check_target(self, address):
        """远端核对原消息的 chat、用户和租户；撤回或不一致目标禁止发送。"""
        from lark_channel.api.im.v1.model.get_message_request import GetMessageRequest

        request = (
            GetMessageRequest.builder()
            .message_id(address["message_id"])
            .user_id_type("open_id")
            .build()
        )
        response = await self.client.im.v1.message.aget(request)
        if response.code in {230002, 230011, 230013}:
            return Receipt("suppressed", error_class="original_message_unavailable")
        if response.code != 0:
            return Receipt("retry", error_class="target_check_rejected")
        items = getattr(response.data, "items", None) or []
        if len(items) != 1:
            return Receipt("suppressed", error_class="original_message_missing")
        item = items[0]
        sender = getattr(item, "sender", None)
        if (
            item.message_id != address["message_id"]
            or item.chat_id != address["chat_id"]
            or item.deleted
            or sender is None
            or sender.sender_type != "user"
            or sender.id_type != "open_id"
            or sender.id != address["open_id"]
            or sender.tenant_key != address["tenant_key"]
        ):
            return Receipt("suppressed", error_class="original_message_binding_changed")
        return None

    async def send(self, claim):
        """一次 areply 对应一次固定分片操作；SDK 不自动换 UUID 或改为 create。"""
        from lark_channel.api.im.v1.model.reply_message_request import ReplyMessageRequest
        from lark_channel.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": claim["content"]}, ensure_ascii=False))
            .uuid(claim["send_key"])
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(claim["address"]["message_id"])
            .request_body(body)
            .build()
        )
        response = await self.client.im.v1.message.areply(request)
        data = response.data
        if response.code == 0:
            message_id = getattr(data, "message_id", None)
            if (
                message_id
                and len(message_id) <= 128
                and getattr(data, "chat_id", None) == claim["address"]["chat_id"]
                and getattr(data, "parent_id", None) == claim["address"]["message_id"]
            ):
                return Receipt("sent", message_id=message_id)
            return Receipt("uncertain", error_class="receipt_incomplete_or_mismatched")
        if response.code in {230020, 99991400}:
            return Receipt("retry", error_class="rate_limited")
        if response.code in {230001, 230002, 230006, 230011, 230013, 230025, 230027, 230028}:
            return Receipt("failed", error_class="reply_rejected")
        return Receipt("uncertain", error_class="unclassified_reply_result")
