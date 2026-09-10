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


@pytest.mark.asyncio
async def test_sdk_cardkit_create_reply_and_update_one_message(sdk):
    """检查真实 SDK 的 CardKit 路径、内容、UUID 和递增 sequence。"""
    sdk.claim.update(
        message_type="card",
        content=json.dumps({"schema": "2.0", "body": {"elements": []}}),
        card_id="card-1",
        target_message_id=None,
        sequence=2,
    )
    sdk.reply = {"code": 0, "data": {"card_id": "card-1"}}
    assert await sdk.gateway.create_card(sdk.claim["content"]) == "card-1"
    request = sdk.calls[-1]
    assert request.url.path == "/open-apis/cardkit/v1/cards"
    assert json.loads(request.content) == {"type": "card_json", "data": sdk.claim["content"]}
    sdk.reply = {
        "code": 0,
        "data": {"message_id": "om_receipt", "chat_id": "oc_chat", "parent_id": "om_original"},
    }
    assert (await sdk.gateway.send(sdk.claim)).message_id == "om_receipt"
    sent = json.loads(sdk.calls[-1].content)
    assert sent["msg_type"] == "interactive"
    assert json.loads(sent["content"]) == {"type": "card", "data": {"card_id": "card-1"}}
    sdk.claim.update(target_message_id="om_receipt", sequence=3)
    sdk.reply = {"code": 0}
    assert (await sdk.gateway.send(sdk.claim)).message_id == "om_receipt"
    request = sdk.calls[-1]
    assert request.method == "PUT" and request.url.path == "/open-apis/cardkit/v1/cards/card-1"
    body = json.loads(request.content)
    assert body["sequence"] == 3 and body["uuid"] == sdk.claim["send_key"]
    assert body["card"] == {"type": "card_json", "data": sdk.claim["content"]}


@pytest.mark.asyncio
async def test_real_channel_callback_waits_for_durable_business_ack(monkeypatch):
    """同一个官方 WS Channel 的同步入口返回事务结果；未走 SDK 的异步空回包。"""
    import asyncio

    from lark_channel.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

    from financeclaw.bff.channels.feishu import FeishuChannelAdapter

    received = []

    async def handle(raw):
        """模拟具有提交等待点的业务受理。"""
        await asyncio.sleep(0)
        received.append(raw)
        return {"toast": {"type": "info", "content": "已持久受理"}}

    async def shutdown():
        """模拟无普通消息需要排空的服务。"""
        return None

    adapter = FeishuChannelAdapter(
        SimpleNamespace(card_actions=SimpleNamespace(handle=handle), shutdown=shutdown),
        app_id="app",
        app_secret="synthetic",
        allowed_open_ids=frozenset({"user"}),
        max_concurrency=2,
    )
    channel = await asyncio.to_thread(adapter._build_channel)
    adapter._application_loop = asyncio.get_running_loop()
    adapter._accepting_messages = True
    event = P2CardActionTrigger(
        {
            "header": {"app_id": "app", "event_id": "event", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "user", "tenant_key": "tenant"},
                "context": {"open_chat_id": "chat", "open_message_id": "message"},
                "action": {"value": {"op": "answer"}, "form_value": {"f0": "合成"}},
            },
        }
    )
    response = await asyncio.to_thread(channel._on_p2_card_action_trigger, event)
    assert response.toast.content == "已持久受理" and len(received) == 1
    assert received[0]["event"]["operator"]["tenant_key"] == "tenant"
    assert received[0]["event"]["action"]["form_value"] == {"f0": "合成"}
    await adapter.stop()
    unavailable = await asyncio.to_thread(channel._on_p2_card_action_trigger, event)
    assert unavailable.toast.type == "warning" and len(received) == 1


@pytest.mark.asyncio
async def test_callback_capacity_and_shutdown_drain():
    """并发上限拒绝额外回调，关闭必须等待已桥接的持久受理结束。"""
    import asyncio

    from financeclaw.bff.channels.feishu import FeishuChannelAdapter

    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(raw):
        """模拟具有提交等待点的业务受理。"""
        entered.set()
        await release.wait()
        return {"toast": {"type": "info", "content": "已提交"}}

    async def shutdown():
        """模拟无普通消息需要排空的服务。"""
        return None

    adapter = FeishuChannelAdapter(
        SimpleNamespace(card_actions=SimpleNamespace(handle=handle), shutdown=shutdown),
        app_id="app",
        app_secret="synthetic",
        allowed_open_ids=frozenset({"user"}),
        max_concurrency=1,
    )
    adapter._application_loop = asyncio.get_running_loop()
    adapter._accepting_messages = True
    first = asyncio.create_task(asyncio.to_thread(adapter._accept_card, {}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not first.done()
    second = await asyncio.to_thread(adapter._accept_card, {})
    assert second["toast"]["type"] == "warning"
    stopping = asyncio.create_task(adapter.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    assert (await first)["toast"]["content"] == "已提交"
    await stopping
    assert not adapter._card_futures
