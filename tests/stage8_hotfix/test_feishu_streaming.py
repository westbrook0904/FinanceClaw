"""飞书正文必须在原生任务结束前投递，重连、交互和停止不得串入旧文字。"""

import asyncio
import json

import pytest
from langgraph_sdk.schema import StreamPart
from sqlalchemy import select

from financeclaw.api.application.turns.answer_stream import PREVIEW_BYTES, TurnAnswerStream, consume
from financeclaw.integrations.notifications.feishu import Receipt
from financeclaw.integrations.notifications.repository import NotificationRepository
from financeclaw.integrations.notifications.worker import deliver
from financeclaw.shared.channels.feishu.answers import answer_cards
from financeclaw.shared.notifications.tables import NotificationDeliveryRow as Delivery
from financeclaw.shared.notifications.tables import NotificationEventRow as Event
from financeclaw.shared.notifications.tables import NotificationTargetRow as Target
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from financeclaw.shared.turns.types import export
from tests.stage8_hotfix.test_feishu_cards import callback, fresh, publish
from tests.stage8_hotfix.test_feishu_clarification import ask
from tests.stage10.runtime import final_state, tick
from tests.stage10.runtime import runtime as runtime


def chunk(text, cursor="1", *, message_id="answer", metadata=None, **kwargs):
    """模拟 SDK 解码后的原生根模型 token，允许覆盖隔离和工具字段。"""
    return StreamPart(
        "messages",
        [
            {"type": "AIMessageChunk", "id": message_id, "content": text, **kwargs},
            metadata or {"langgraph_node": "model", "langgraph_checkpoint_ns": "model:root"},
        ],
        cursor,
    )


async def started(runtime):
    """真实飞书受理及命令提交后，隔离生命周期的自动订阅以控制传输时序。"""
    service, accepted, event, gateway = await fresh(runtime)
    await tick(runtime)
    await runtime.turns.lifecycle.stop()
    with runtime.turns.sessions() as session:
        root = session.get(ConversationTurnRow, accepted.turn_id)
        command = session.get(TurnCommandRow, root.current_command_id)
        return service, accepted, event, gateway, export(root), export(command)


def test_filters_private_outputs_and_bounds_preview():
    """子 Agent、工具参数、思考块不进入展示；中文长正文按 UTF-8 边界截断。"""
    state = {"text": ""}
    consume(
        state,
        chunk(
            "secret",
            metadata={
                "langgraph_node": "model",
                "langgraph_checkpoint_ns": "tools:root|model:child",
            },
        ),
    )
    consume(
        state,
        chunk(
            "summary",
            "2",
            metadata={"langgraph_node": "summary", "langgraph_checkpoint_ns": "summary:root"},
        ),
    )
    consume(
        state,
        chunk(
            [
                {"type": "reasoning", "text": "secret thinking"},
                {"type": "text", "text": "公开"},
            ],
            "3",
            additional_kwargs={"reasoning_content": "private"},
        ),
    )
    assert state["text"] == "公开"
    consume(state, chunk("不能展示", "4", tool_call_chunks=[{"args": "private"}]))
    consume(state, chunk("仍不能展示", "5"))
    assert state["text"] == "" and state["blocked"]
    consume(state, chunk("中" * PREVIEW_BYTES, "6", message_id="new-answer"))
    assert len(state["text"].encode()) <= PREVIEW_BYTES and state["truncated"]
    before = dict(state)
    consume(state, chunk("重复", "6", message_id="new-answer"))
    assert state == before


@pytest.mark.asyncio
async def test_tokens_arrive_before_completion_and_reconnect_without_duplication(runtime):
    """模型尚未完成即可看到部分正文；重建观察者使用持久游标继续更新原卡。"""
    _, accepted, _, gateway, turn, command = await started(runtime)
    queue = asyncio.Queue()
    subscriptions = []

    async def stream(thread_id, run_id, **kwargs):
        """直到测试显式结束才关闭运行，记录实际 SDK 订阅参数。"""
        subscriptions.append((thread_id, run_id, kwargs))
        while (part := await queue.get()) is not None:
            yield part

    runtime.client.join_stream = stream
    answers = TurnAnswerStream(runtime.turns, runtime.turns.lifecycle.native, interval=0.01)
    observer = asyncio.create_task(answers.join(turn, command))
    await queue.put(chunk("**逐步", "1"))
    async with asyncio.timeout(3):
        while answers.read(turn, command).get("text") != "**逐步":
            await asyncio.sleep(0.01)
    event, gateway = await publish(runtime, gateway)
    assert "**逐步" in gateway.calls[-1]["content"]
    assert runtime.client.values[command["native_run_id"]]["status"] == "running"
    assert not observer.done()
    observer.cancel()
    await asyncio.gather(observer, return_exceptions=True)
    answers = TurnAnswerStream(runtime.turns, runtime.turns.lifecycle.native, interval=0.01)
    observer = asyncio.create_task(answers.join(turn, command))
    await queue.put(chunk("输出**", "2"))
    await queue.put(None)
    await observer
    assert subscriptions[0][2] == {"last_event_id": "-1", "cancel_on_disconnect": False}
    assert subscriptions[1][2]["last_event_id"] == "1"
    assert answers.read(turn, command)["text"] == "**逐步输出**"
    final_state(runtime, accepted, content="**完整回复**")
    await tick(runtime)
    final, gateway = await publish(runtime, gateway)
    assert final.revision > event.revision
    assert final.payload["status"] == "completed" and "stream" not in final.payload
    assert "**完整回复**" in gateway.calls[-1]["content"]
    assert {c["target_message_id"] for c in gateway.calls[1:-1]} == {"message-1"}
    assert runtime.client.calls[0]["stream_resumable"] is True
    assert runtime.client.calls[0]["stream_mode"] == ["messages-tuple"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "clarify", "failed", "mute"])
async def test_late_tokens_cannot_overwrite_lifecycle(runtime, ending):
    """停止、澄清、失败及静音使旧预览失效，状态卡仍能使用递增序号更新。"""
    service, accepted, event, gateway, turn, command = await started(runtime)
    answers = runtime.turns.lifecycle.answers
    state = answers.read(turn, command)
    consume(state, chunk("不完整正文"))
    assert answers.save(turn, command, state, expected_cursor=None)
    event, gateway = await publish(runtime, gateway)
    if ending == "stop":
        await service.card_actions.handle(callback(event, "cancel"))
        await tick(runtime)
    elif ending == "clarify":
        await ask(runtime, accepted)
    elif ending == "failed":
        runtime.client.values[command["native_run_id"]]["status"] = "error"
        await tick(runtime)
    else:
        with runtime.turns.sessions.begin() as session:
            session.scalar(select(Target)).active = False
    consume(state, chunk("迟到文字", "2"))
    assert not answers.save(turn, command, state, expected_cursor="1")
    if ending != "mute":
        final, gateway = await publish(runtime, gateway)
        assert final.revision > event.revision and "stream" not in final.payload
        assert "不完整正文" not in gateway.calls[-1]["content"]
        assert (
            final.payload["status"]
            == {"stop": "cancelled", "clarify": "waiting", "failed": "failed"}[ending]
        )


@pytest.mark.asyncio
async def test_cursor_cas_and_coalescing_do_not_change_business_revision(runtime):
    """并发旧订阅者不能倒写；积压预览只消费最新版本，不制造业务状态修订。"""
    _, _, _, gateway, turn, command = await started(runtime)
    answers = runtime.turns.lifecycle.answers
    state = answers.read(turn, command)
    for index in range(10):
        cursor = state.get("cursor")
        consume(state, chunk(str(index), str(index)))
        assert answers.save(turn, command, state, expected_cursor=cursor)
    assert not answers.save(turn, command, {**state, "text": "旧值"}, expected_cursor="0")
    assert answers.read(turn, {**command, "command_id": "old-command"}) is None
    event, gateway = await publish(runtime, gateway)
    assert "0123456789" in gateway.calls[-1]["content"]
    assert len(gateway.calls) == 2
    with runtime.turns.sessions() as session:
        assert session.get(ConversationTurnRow, turn["turn_id"]).revision == turn["revision"]
        assert event.revision > turn["revision"]
        assert not list(session.scalars(select(Event).where(Event.kind == "terminal")))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_new_tokens_during_target_check_do_not_starve_delivery(runtime, cancel):
    """复现线上时序：检查原消息期间持续产生新版正文，已领取的片段仍须送达。"""
    service, _, event, gateway, turn, command = await started(runtime)
    answers = runtime.turns.lifecycle.answers
    state = answers.read(turn, command)
    consume(state, chunk("第一段", "1"))
    assert answers.save(turn, command, state, expected_cursor=None)
    repository = NotificationRepository(
        runtime.turns.sessions, app_id="app", allowed_open_ids=frozenset({"user"})
    )
    while repository.materialize():
        pass
    checking, release = asyncio.Event(), asyncio.Event()

    async def slow_check(address):
        """阻塞检查，确保新版正文恰好在 validate 与 prepare 之间落库。"""
        checking.set()
        await release.wait()

    gateway.check_target = slow_check
    claim = repository.claim("slow-sender", lease_seconds=60)
    sending = asyncio.create_task(deliver(repository, gateway, claim, runtime.turns.settings))
    await asyncio.wait_for(checking.wait(), timeout=3)
    try:
        for index in range(2, 8):
            cursor = state["cursor"]
            consume(state, chunk("追加正文", str(index)))
            assert answers.save(turn, command, state, expected_cursor=cursor)
        if cancel:
            await service.card_actions.handle(callback(event, "cancel"))
    finally:
        release.set()
        await asyncio.wait_for(sending, timeout=3)
    if cancel:
        assert len(gateway.calls) == 1
        with runtime.turns.sessions() as session:
            assert session.get(Delivery, claim["delivery_id"]).status == "suppressed"
        return
    assert len(gateway.calls) == 2
    assert "第一段" in gateway.calls[-1]["content"]
    assert gateway.calls[-1]["target_message_id"] == "message-1"
    with runtime.turns.sessions() as session:
        assert session.get(Delivery, claim["delivery_id"]).status == "sent"
        assert session.get(ConversationTurnRow, turn["turn_id"]).status == "running"
    # 发送完成后才合并积压片段，不改写已经发出的内容和发送键。
    _, gateway = await publish(runtime, gateway)
    assert state["text"] in gateway.calls[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("long_answer", [False, True])
async def test_final_answer_stays_in_original_card_without_duplicate_first_part(
    runtime, long_answer
):
    """原任务卡直接定稿；长回复只补发后续分片，首片沿用已确认的原卡回执。"""
    _, accepted, _, gateway, turn, command = await started(runtime)
    text = "**完整正文**" if not long_answer else "```text\n" + "长正文\n" * 3000 + "```"
    final_state(runtime, accepted, content=text)
    await tick(runtime)
    _, gateway = await publish(runtime, gateway)
    cards = answer_cards(text)
    assert len(gateway.calls) == 1 + len(cards)
    assert gateway.calls[1]["target_message_id"] == "message-1"
    assert json.loads(gateway.calls[1]["content"]) == cards[0]
    assert "本轮已完成" not in gateway.calls[1]["content"]
    with runtime.turns.sessions() as session:
        parts = list(
            session.scalars(
                select(Delivery).join(Event).where(Event.kind == "terminal").order_by(Delivery.part)
            )
        )
        assert all(part.status == "sent" for part in parts)
        assert parts[0].message_id == "message-1" and parts[0].attempts == 0
        assert all(part.attempts == 1 for part in parts[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "uncertain"])
async def test_final_card_failure_keeps_independent_complete_answer_delivery(runtime, status):
    """原卡定稿失败或回执丢失时，最终文本仍可通过独立的既有投递责任送达。"""
    _, accepted, _, gateway, turn, command = await started(runtime)
    final_state(runtime, accepted, content="**确认的最终答案**")
    await tick(runtime)
    gateway.receipts = [Receipt(status, error_class="synthetic_update_failure")]
    _, gateway = await publish(runtime, gateway)
    assert len(gateway.calls) == 3
    assert gateway.calls[-1]["target_message_id"] is None
    assert "**确认的最终答案**" in gateway.calls[-1]["content"]
    with runtime.turns.sessions() as session:
        final = session.scalar(select(Delivery).join(Event).where(Event.kind == "terminal"))
        assert final.status == "sent" and final.attempts == 1


@pytest.mark.asyncio
async def test_real_graph_token_protocol_matches_public_filter():
    """真实 create_agent 流的根命名空间与消息类型能通过正文过滤。"""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    graph = create_agent(FakeListChatModel(responses=["逐字输出"]))
    state = {"text": ""}
    index = 0
    async for message, metadata in graph.astream(
        {"messages": [{"role": "user", "content": "synthetic"}]}, stream_mode="messages"
    ):
        index += 1
        consume(state, StreamPart("messages", [message.model_dump(), metadata], str(index)))
    assert index > 1 and state["text"] == "逐字输出"
    assert "逐字输出" in json.dumps(state, ensure_ascii=False)


@pytest.mark.asyncio
async def test_inprocess_sdk_does_not_buffer_until_stream_ends(runtime, monkeypatch):
    """使用已安装的 Agent Server ASGI 传输和 SDK，源连接未结束时完成卡片投递。"""
    import httpx
    from langgraph_sdk.client import LangGraphClient

    transport_module = pytest.importorskip("langgraph_api.asgi_transport")
    loop_module = pytest.importorskip("langgraph_api.asyncio")
    # 单测试已在 ASGI 主循环中；替换调度入口，避免引入独立服务的 Redis 配置。
    monkeypatch.setattr(loop_module, "call_soon_in_main_loop", asyncio.create_task)
    _, _, _, gateway, turn, command = await started(runtime)
    release = asyncio.Event()
    native = runtime.turns.lifecycle.native
    receipt = runtime.client.values[command["native_run_id"]]

    async def app(scope, receive, send):
        """原生 HTTP 路由只替换执行端；SSE 编解码和进程内传输保持真实。"""
        is_stream = scope["path"].endswith("/stream")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream" if is_stream else b"application/json")
                ],
            }
        )
        if is_stream:
            assert dict(scope["headers"])[b"last-event-id"] == b"-1"
            part = chunk("尚未完成的正文", "native-event-1")
            body = f"id: {part.id}\nevent: messages\ndata: {json.dumps(part.data)}\n\n"
            await send({"type": "http.response.body", "body": body.encode(), "more_body": True})
            await release.wait()
        await send(
            {
                "type": "http.response.body",
                "body": b"" if is_stream else json.dumps(receipt).encode(),
            }
        )

    async with httpx.AsyncClient(
        transport=transport_module.ASGITransport(app=app, root_path="/noauth"),
        base_url="http://api",
    ) as client:
        monkeypatch.setattr(native, "client", LangGraphClient(client))
        answers = TurnAnswerStream(runtime.turns, native, interval=0.01)
        observer = asyncio.create_task(answers.join(turn, command))
        try:
            async with asyncio.timeout(3):
                while answers.read(turn, command).get("text") != "尚未完成的正文":
                    if observer.done():
                        await observer
                    await asyncio.sleep(0.01)
            assert not observer.done() and not release.is_set()
            _, gateway = await publish(runtime, gateway)
            assert "尚未完成的正文" in gateway.calls[-1]["content"]
        finally:
            release.set()
            await asyncio.wait_for(observer, timeout=3)
