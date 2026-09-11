"""真实 create_agent 图上的批次准入测试，不以单独调用 middleware 代替顺序验收。"""

import json
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.shared.turns.types import ExecutionConflict
from tests.stage1.test_agent import components_with_tools, context


class BatchModel(OfflineFinanceModel):
    """首次返回整个批次，下一次原样呈现收到的全部工具结果。"""

    calls: list[dict[str, Any]]

    def _generate(self, messages, *args, **kwargs) -> ChatResult:
        """只在第一次模型输出发起调用，随后收集全部结果。"""
        results = [message for message in messages if isinstance(message, ToolMessage)]
        answer = (
            AIMessage(content="\n".join(str(message.content) for message in results))
            if results
            else AIMessage(content="", tool_calls=self.calls)
        )
        return ChatResult(generations=[ChatGeneration(message=answer)])


def call(name: str, index: int, **args: Any) -> dict[str, Any]:
    """为同名不同参数的调用生成独立身份。"""
    return {"name": name, "id": f"call-{index}", "args": args, "type": "tool_call"}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_reads_merge_all_results_before_next_model(asynchronous: bool) -> None:
    """同步和异步图都必须收齐同批次结果，再让模型决策。"""
    components, _ = components_with_tools()
    graph = components.agent_factory.build(
        components.default_agent_profile,
        model=BatchModel(
            calls=[
                call("calculate", 1, operation="add", left=1, right=2),
                call("calculate", 2, operation="add", left=3, right=4),
            ]
        ),
    )
    kwargs = {"config": {"configurable": {"thread_id": "reads"}}, "context": context("tools:read")}
    inputs = {"messages": [{"role": "user", "content": "calculate two independent facts"}]}
    result = (
        await graph.ainvoke(inputs, **kwargs) if asynchronous else graph.invoke(inputs, **kwargs)
    )
    messages = result["messages"]
    assert [m.tool_call_id for m in messages if isinstance(m, ToolMessage)] == ["call-1", "call-2"]
    assert "3" in messages[-1].content and "7" in messages[-1].content


@pytest.mark.parametrize("kind", ["writes", "composites", "mixed", "directive"])
def test_unsupported_batch_is_rejected_before_hitl_and_dispatch(kind: str) -> None:
    """不支持的批次不能产生审批、子图或部分执行。"""
    components, audit = components_with_tools()
    read = call("calculate", 1, operation="add", left=1, right=1)
    write = call("watchlist_add", 2, symbol="AAPL", note="test")
    worker_call = call("call_agent__market_research_agent", 3, task="research AAPL")
    batches = {
        "writes": [write, {**write, "id": "another-write"}],
        "composites": [worker_call, {**worker_call, "id": "another-worker"}],
        "mixed": [read, worker_call],
        "directive": [read, {**read, "id": "duplicate-read"}],
    }
    graph = components.agent_factory.build(
        components.default_agent_profile, model=BatchModel(calls=batches[kind])
    )
    message = (
        '/tool calculate {"operation":"add","left":1,"right":1}'
        if kind == "directive"
        else "do this"
    )
    result = graph.invoke(
        {"messages": [{"role": "user", "content": message}]},
        config={"configurable": {"thread_id": kind}},
        context=context("market:read", "watchlist:write").model_copy(
            update={"conversation_id": "conversation-batch"}
        ),
    )
    assert not result.get("__interrupt__")
    results = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == 2
    assert all(m.status == "error" and "unsupported_tool_batch" in m.content for m in results)
    assert not any(e.decision == "executed" for e in audit.records())


def test_duplicate_call_ids_stop_before_dispatch() -> None:
    """损坏的批次无法匹配结果时受控终止，不先执行再猜测归属。"""
    components, audit = components_with_tools()
    read = call("calculate", 1, operation="add", left=1, right=2)
    graph = components.agent_factory.build(
        components.default_agent_profile, model=BatchModel(calls=[read, dict(read)])
    )
    with pytest.raises(ExecutionConflict, match="duplicate call IDs"):
        graph.invoke(
            {"messages": [{"role": "user", "content": "two reads"}]},
            config={"configurable": {"thread_id": "duplicate-ids"}},
            context=context("tools:read"),
        )
    assert not any(event.decision == "executed" for event in audit.records())


class TwoRoundModel(OfflineFinanceModel):
    """收齐第一批结果后，再构造依赖前一批的调用。"""

    counts: ClassVar[list[int]] = []

    def _generate(self, messages, *args, **kwargs):
        """观察每次模型收到的完整结果数，固定 0 → 2 → 3 的汇合边界。"""
        results = [m for m in messages if isinstance(m, ToolMessage)]
        self.counts.append(len(results))
        if not results:
            answer = AIMessage(
                content="",
                tool_calls=[call("calculate", i, operation="add", left=i, right=i) for i in (1, 2)],
            )
        elif len(results) == 2:
            values = [float(json.loads(str(item.content))["value"]) for item in results]
            answer = AIMessage(
                content="",
                tool_calls=[
                    call("calculate", 3, operation="multiply", left=values[0], right=values[1])
                ],
            )
        else:
            answer = AIMessage(content=str(results[-1].content))
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_dependency_is_executed_in_the_next_model_round(asynchronous):
    """A08/A09：无依赖先并行汇合，有依赖的计算由下一轮确定入参。"""
    components, _ = components_with_tools()
    TwoRoundModel.counts.clear()
    graph = components.agent_factory.build(components.default_agent_profile, model=TwoRoundModel())
    inputs = {"messages": [{"role": "user", "content": "combine two results"}]}
    kwargs = {
        "context": context("tools:read"),
        "config": {"configurable": {"thread_id": "two-rounds"}},
    }
    result = (
        await graph.ainvoke(inputs, **kwargs) if asynchronous else graph.invoke(inputs, **kwargs)
    )
    assert TwoRoundModel.counts == [0, 2, 3]
    assert Decimal(json.loads(result["messages"][-1].content)["value"]) == Decimal("8")
