"""官方 SDK 的实际序列化、HTTP 传输和回执解码；不连接真实飞书。"""

import json
from types import SimpleNamespace

import httpx
import pytest

from financeclaw.bff.notifications.feishu import FeishuNotificationGateway


@pytest.fixture
def sdk(monkeypatch):
    """只替换 HTTP 边界与合成 token；实际 Client、API 模型和 Transport 均运行。"""
    from lark_channel import Client, LogLevel
    from lark_channel.core.token.manager import TokenManager

    monkeypatch.setattr(TokenManager, "get_self_tenant_token", lambda _: "synthetic-token")
    state = SimpleNamespace(
        calls=[],
        reply={
            "code": 0,
            "data": {"message_id": "om_receipt", "chat_id": "oc_chat", "parent_id": "om_original"},
        },
        original={
            "message_id": "om_original",
            "chat_id": "oc_chat",
            "deleted": False,
            "sender": {
                "id": "ou_user",
                "id_type": "open_id",
                "sender_type": "user",
                "tenant_key": "tenant",
            },
        },
        lose=False,
    )

    async def transport(request):
        """观察 SDK 真正产生的 JSON、路径及固定 UUID，注入 HTTP 响应或读超时。"""
        state.calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"items": [state.original]}})
        if state.lose:
            raise httpx.ReadTimeout("synthetic response lost")
        return httpx.Response(200, json=state.reply)

    original_client = httpx.AsyncClient

    def client(**kwargs):
        """让 SDK 每次创建的真实 HTTPX 客户端使用无网络 MockTransport。"""
        return original_client(transport=httpx.MockTransport(transport))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    state.gateway = FeishuNotificationGateway(
        Client.builder()
        .app_id("cli_synthetic")
        .app_secret("synthetic-secret")
        .log_level(LogLevel.ERROR)
        .timeout(2)
        .build()
    )
    state.claim = {
        "address": {
            "app_id": "cli_synthetic",
            "tenant_key": "tenant",
            "open_id": "ou_user",
            "chat_id": "oc_chat",
            "message_id": "om_original",
        },
        "content": "合成正文🧪",
        "send_key": "ba247d65-c9ea-5328-a134-16b1d9187ad7",
    }
    return state


@pytest.mark.asyncio
async def test_real_sdk_request_and_message_receipt(sdk):
    """真实 SDK 仅发一次原消息 reply，UUID 与冻结文本不被高层重写。"""
    assert await sdk.gateway.check_target(sdk.claim["address"]) is None
    receipt = await sdk.gateway.send(sdk.claim)
    assert receipt.status == "sent" and receipt.message_id == "om_receipt"
    assert sdk.calls[0].url.params["user_id_type"] == "open_id"
    assert sdk.calls[1].url.path == "/open-apis/im/v1/messages/om_original/reply"
    body = json.loads(sdk.calls[1].content)
    assert body["uuid"] == sdk.claim["send_key"]
    assert json.loads(body["content"])["text"] == sdk.claim["content"]
    assert body["msg_type"] == "text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status", [(230020, "retry"), (230027, "failed"), (999999, "uncertain")]
)
async def test_sdk_errors_do_not_fallback_or_invent_receipts(sdk, code, status):
    """限流、权限拒绝及未分类响应保留不同责任，不换目标或 create 方法。"""
    sdk.reply = {"code": code, "msg": "synthetic"}
    receipt = await sdk.gateway.send(sdk.claim)
    assert receipt.status == status and receipt.message_id is None
    assert len(sdk.calls) == 1


@pytest.mark.asyncio
async def test_sdk_lost_response_and_incomplete_receipt(sdk):
    """SDK 不偷偷重试；成功缺 ID 或回执归属错误都不是 sent。"""
    sdk.lose = True
    with pytest.raises(httpx.ReadTimeout):
        await sdk.gateway.send(sdk.claim)
    assert len(sdk.calls) == 1
    sdk.lose = False
    sdk.reply = {"code": 0, "data": {"message_id": "wrong", "chat_id": "other-chat"}}
    assert (await sdk.gateway.send(sdk.claim)).status == "uncertain"
    sdk.reply = {"code": 0}
    assert (await sdk.gateway.send(sdk.claim)).status == "uncertain"


@pytest.mark.asyncio
async def test_sdk_rejects_changed_target(sdk):
    """远端原消息变为别的用户或已经撤回时不投递。"""
    sdk.original["sender"]["id"] = "ou_different"
    assert (await sdk.gateway.check_target(sdk.claim["address"])).status == "suppressed"
    sdk.original["sender"]["id"] = "ou_user"
    sdk.original["deleted"] = True
    assert (await sdk.gateway.check_target(sdk.claim["address"])).status == "suppressed"
    assert all(request.method == "GET" for request in sdk.calls)
