"""验证原生工具进度跨根图、子图、重连和飞书投递，且不泄露工具内容。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from langgraph_sdk.schema import StreamPart

from financeclaw.agent_server.middleware.tool_progress import ToolProgressMiddleware
from financeclaw.api.application.turns.answer_stream import TurnAnswerStream, consume
from financeclaw.kernel.tool_progress import TOOL_PROGRESS_LIMIT, ToolProgress
from tests.stage1.test_agent import components_with_tools, context
from tests.stage8_hotfix.test_feishu_cards import publish
from tests.stage8_hotfix.test_feishu_streaming import chunk, started
from tests.stage10.runtime import final_state, tick
from tests.stage10.runtime import runtime as runtime


def progress(status="started", *, call=1, tool_name="market_snapshot", agent="finance_agent"):
    """生成不带参数和工具结果的 SDK custom 事件。"""
    return ToolProgress(
        call_id=f"{call:064x}", agent=agent, tool=tool_name, status=status
    ).model_dump()


def test_progress_projection_filters_raw_data_and_keeps_parallel_calls():
    """只处理声明过的公开事件，工具同名并行不合并，正文和工具结果保持隔离。"""
    state = {"text": "旧的模型前言"}
    consume(state, StreamPart("custom", progress(), "1"))
    consume(state, StreamPart("custom|tools:root|evidence:child", progress(call=2), "2"))
    consume(state, StreamPart("custom", progress("completed"), "3"))
    assert [item["status"] for item in state["tools"]] == ["completed", "started"]
    assert state["text"] == ""
    consume(state, StreamPart("custom", {**progress(), "args": "PRIVATE"}, "4"))
    consume(state, StreamPart("custom", {"type": "raw", "output": "PRIVATE"}, "5"))
    consume(state, StreamPart("custom", {**progress(), "tool": "<at id=all>"}, "6"))
    consume(state, StreamPart("messages|tools:child", [{"content": "PRIVATE"}, {}], "7"))
    assert [item["status"] for item in state["tools"]] == ["completed", "started"]
    assert "PRIVATE" not in json.dumps(state)
    for i in range(3, TOOL_PROGRESS_LIMIT + 10):
        consume(state, StreamPart("custom", progress("completed", call=i), str(i + 10)))
    assert len(state["tools"]) == TOOL_PROGRESS_LIMIT
    assert any(item["call_id"] == f"{2:064x}" for item in state["tools"])
    consume(state, chunk("最终正文", "100", message_id="final"))
    assert state["text"] == "最终正文"


class ToolModel(FakeMessagesListChatModel):
    """固定调用序列用于真实 create_agent 协议测试，不调用外部模型。"""

    def bind_tools(self, tools, **kwargs):
        """保留预设模型回应，工具执行仍交给框架原生 ToolNode。"""
        return self


def model_for(name, *, parallel=False, answer="公开的最终答案"):
    """同名工具用不同调用 ID 验证实际并行执行，第二轮直接回答。"""
    return ToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "id": f"call-{i}", "args": {}}
                    for i in range(2 if parallel else 1)
                ],
            ),
            AIMessage(content=answer),
        ]
    )


@pytest.mark.asyncio
async def test_real_nested_graph_emits_parallel_tool_starts_before_results():
    """子图 ainvoke 沿用父配置，两个工具开始事件在阻塞解除前均到达根订阅。"""
    release = asyncio.Event()

    @tool
    async def leaf() -> str:
        """模拟仍在等待外部结果的异步工具。"""
        await release.wait()
        return "PRIVATE TOOL RESULT"

    child = create_agent(
        model_for("leaf", parallel=True, answer="PRIVATE CHILD ANSWER"),
        tools=[leaf],
        middleware=[ToolProgressMiddleware("worker", ["leaf"])],
    )

    @tool
    async def delegate(runtime: ToolRuntime) -> str:
        """和生产 Worker 一样，在原生运行配置中直接调用子图。"""
        await child.ainvoke(
            {"messages": [{"role": "user", "content": "PRIVATE INPUT"}]}, config=runtime.config
        )
        return "PRIVATE DELEGATION RESULT"

    graph = create_agent(
        model_for("delegate"),
        tools=[delegate],
        middleware=[ToolProgressMiddleware("root", ["delegate"])],
    )
    events, state = [], {"text": ""}
    index = 0
    async with asyncio.timeout(5):
        async for ns, mode, data in graph.astream(
            {"messages": [{"role": "user", "content": "合成请求"}]},
            stream_mode=["messages", "custom"],
            subgraphs=True,
        ):
            index += 1
            payload = [data[0].model_dump(), data[1]] if mode == "messages" else data
            consume(
                state, StreamPart(mode + ("|" + "|".join(ns) if ns else ""), payload, str(index))
            )
            if mode == "custom":
                events.append(data)
                if len(events) == 3:
                    assert [event["status"] for event in events] == ["started"] * 3
                    assert [event["tool"] for event in events] == ["delegate", "leaf", "leaf"]
                    assert len({event["call_id"] for event in events}) == 3
                    release.set()
    assert len(events) == 6
    assert all(item["status"] == "completed" for item in state["tools"])
    assert state["text"] == "公开的最终答案"
    assert "PRIVATE" not in json.dumps(events)
    assert "PRIVATE" not in json.dumps(state)


def test_factory_progress_wraps_actual_retry_as_one_logical_call():
    """实际 Factory 的自动重试只呈现一个开始和最终完成，两个真实尝试仍执行。"""
    from financeclaw.agent_server.agents.offline import OfflineFinanceModel
    from financeclaw.agent_server.tools.local import MarketSnapshotTool

    market = MarketSnapshotTool(fail_first=1)
    components, _ = components_with_tools(market=market)
    graph = components.agent_factory.build(
        components.default_agent_profile, model=OfflineFinanceModel()
    )
    events = list(
        graph.stream(
            {"messages": [{"role": "user", "content": "read AAPL"}]},
            config={"configurable": {"thread_id": "progress-retry"}},
            context=context("market:read"),
            stream_mode="custom",
        )
    )
    assert market.call_count == 2
    assert [event["status"] for event in events] == ["started", "completed"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "outcome", ["error_message", "error_command", "exception", "interrupt", "cancel"]
)
@pytest.mark.asyncio
async def test_terminal_progress_preserves_errors_and_control_flow(asynchronous, outcome):
    """失败回执不能显示成功；中断和取消不吞掉、不伪装成工具执行错误。"""
    if outcome == "cancel" and not asynchronous:
        return
    events = []
    middleware = ToolProgressMiddleware("root", ["leaf"])
    request = SimpleNamespace(
        tool_call={"name": "leaf", "id": "call", "args": {"private": "PRIVATE"}},
        runtime=SimpleNamespace(config={}, stream_writer=events.append),
    )
    error = ToolMessage(content="PRIVATE", tool_call_id="call", status="error")
    result = error if outcome == "error_message" else Command(update={"messages": [error]})
    raised = {
        "exception": ValueError,
        "interrupt": GraphInterrupt,
        "cancel": asyncio.CancelledError,
    }

    def handler(request):
        """返回原始错误消息，或抛出框架真实控制流异常。"""
        if outcome in raised:
            raise raised[outcome]()
        return result

    async def async_handler(request):
        """异步返回与同步处理保持一致。"""
        return handler(request)

    async def invoke():
        """选择实际的同步或异步中间件入口。"""
        if asynchronous:
            return await middleware.awrap_tool_call(request, async_handler)
        return middleware.wrap_tool_call(request, handler)

    if outcome in raised:
        with pytest.raises(raised[outcome]):
            await invoke()
    else:
        assert await invoke() is result
    assert [event["status"] for event in events] == [
        "started",
        {"interrupt": "interrupted", "cancel": "cancelled"}.get(outcome, "failed"),
    ]
    assert "PRIVATE" not in json.dumps(events)


@pytest.mark.asyncio
async def test_tool_progress_updates_feishu_while_running_and_reconnects(runtime):
    """没有模型正文时也更新原卡；重连保存工具状态，完成后原卡仍交付最终正文。"""
    _, accepted, _, gateway, turn, command = await started(runtime)
    queue, subscriptions = asyncio.Queue(), []

    async def stream(thread_id, run_id, **kwargs):
        """保持原生运行未结束，精确控制开始、完成及重连时序。"""
        subscriptions.append(kwargs)
        while (part := await queue.get()) is not None:
            yield part

    runtime.client.join_stream = stream
    answers = TurnAnswerStream(runtime.turns, runtime.turns.lifecycle.native, interval=0.01)
    observer = asyncio.create_task(answers.join(turn, command))
    await queue.put(StreamPart("custom", progress(), "1"))
    try:
        async with asyncio.timeout(3):
            while not answers.read(turn, command).get("tools"):
                await asyncio.sleep(0.01)
        await publish(runtime, gateway)
        assert "正在使用" in gateway.calls[-1]["content"]
        assert "market_snapshot" in gateway.calls[-1]["content"]
        assert gateway.calls[-1]["target_message_id"] == "message-1"
        assert not observer.done()
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    observer = asyncio.create_task(answers.join(turn, command))
    await queue.put(StreamPart("custom", progress("completed"), "2"))
    await queue.put(None)
    await observer
    await publish(runtime, gateway)
    assert subscriptions[-1]["last_event_id"] == "1"
    assert "已完成" in gateway.calls[-1]["content"]
    assert answers.read(turn, command)["tools"][0]["status"] == "completed"
    final_state(runtime, accepted, content="**最终答案**")
    await tick(runtime)
    await publish(runtime, gateway)
    assert "最终答案" in gateway.calls[-1]["content"]
    assert "market_snapshot" not in gateway.calls[-1]["content"]
    assert gateway.calls[-1]["target_message_id"] == "message-1"
