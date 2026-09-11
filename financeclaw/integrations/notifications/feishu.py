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

        if claim.get("message_type") == "card" and claim.get("target_message_id"):
            return await self.update_card(claim)
        content = (
            {"type": "card", "data": {"card_id": claim["card_id"]}}
            if claim.get("message_type") == "card"
            else {"text": claim["content"]}
        )
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("interactive" if claim.get("message_type") == "card" else "text")
            .content(json.dumps(content, ensure_ascii=False))
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

    async def create_card(self, content):
        """创建尚未发布的 CardKit 实例；返回可持久保存的 ID。"""
        from lark_channel.api.cardkit.v1.model.create_card_request import CreateCardRequest
        from lark_channel.api.cardkit.v1.model.create_card_request_body import CreateCardRequestBody

        response = await self.client.cardkit.v1.card.acreate(
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(content).build())
            .build()
        )
        if response.code != 0 or not getattr(response.data, "card_id", None):
            raise ValueError("card creation rejected")
        return response.data.card_id

    async def update_card(self, claim):
        """固定 UUID/sequence 的全量更新，卡片操作不依赖临时回调 token。"""
        from lark_channel.api.cardkit.v1.model.card import Card
        from lark_channel.api.cardkit.v1.model.update_card_request import UpdateCardRequest
        from lark_channel.api.cardkit.v1.model.update_card_request_body import UpdateCardRequestBody

        response = await self.client.cardkit.v1.card.aupdate(
            UpdateCardRequest.builder()
            .card_id(claim["card_id"])
            .request_body(
                UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(claim["content"]).build())
                .uuid(claim["send_key"])
                .sequence(claim["sequence"])
                .build()
            )
            .build()
        )
        if response.code == 0:
            return Receipt("sent", message_id=claim["target_message_id"])
        if response.code in {230020, 99991400}:
            return Receipt("retry", error_class="rate_limited")
        return Receipt("uncertain", error_class="card_update_unconfirmed")
