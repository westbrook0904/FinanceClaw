"""用真实根图验证限额收尾、整批回执和跨 Turn 状态隔离。"""

from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from tests.stage1.test_agent import components_with_tools, context


class BudgetModel(OfflineFinanceModel):
    """持续提出工具请求；仅在真正撤下工具时生成最终回答。"""

    batches: tuple[int, ...] = (3, 3, 3, 2, 2)
    observed: ClassVar[list] = []

    def _generate(self, messages, *args, **kwargs):
        """检查每次请求配对完整，记录实际可见工具。"""
        pending = set()
        for message in messages:
            if isinstance(message, AIMessage):
                assert not pending
                pending.update(call["id"] for call in message.tool_calls)
            elif isinstance(message, ToolMessage):
                assert message.tool_call_id in pending
                pending.remove(message.tool_call_id)
        assert not pending
        index = len(type(self).observed)
        type(self).observed.append(set(self._bound_tool_names))
        if not self._bound_tool_names:
            answer = AIMessage(content="根据已有查询结果回答；尚未核实的部分已说明。")
        else:
            size = self.batches[min(index, len(self.batches) - 1)]
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "calculate",
                        "id": f"batch-{index}-{i}",
                        "args": {"operation": "add", "left": i, "right": 1},
                        "type": "tool_call",
                    }
                    for i in range(size)
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_thirteen_attempts_execute_only_eleven_and_finish(asynchronous):
    """11+2 的最后一批完全不执行，回执配对后只用一次模型收尾。"""
    components, audit = components_with_tools()
    BudgetModel.observed = []
    graph = components.agent_factory.build(components.default_agent_profile, model=BudgetModel())
    args = {"config": {"configurable": {"thread_id": "budget"}}, "context": context("tools:read")}
    inputs = {"messages": [{"role": "user", "content": "比较几个方案"}]}
    result = await graph.ainvoke(inputs, **args) if asynchronous else graph.invoke(inputs, **args)
    assert len(BudgetModel.observed) == 6
    assert not BudgetModel.observed[-1]
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(receipts) == 13 and sum(m.status == "error" for m in receipts) == 2
    assert sum(e.decision == "executed" for e in audit.records()) == 11
    assert "已有查询结果" in result["messages"][-1].content


@pytest.mark.parametrize("reject_batches", [False, True])
def test_last_model_allowance_finishes_even_after_rejected_batches(reject_batches):
    """批次拒绝也计入模型次数，最后一次不再请求工具。"""
    components, audit = components_with_tools()
    profile = components.default_agent_profile.model_copy(
        update={
            "max_model_calls": 3,
            "max_tool_batch": 1 if reject_batches else 8,
        }
    )
    BudgetModel.observed = []
    graph = components.agent_factory.build(profile, model=BudgetModel(batches=(2,)))
    result = graph.invoke(
        {"messages": [{"role": "user", "content": "继续比较"}]},
        config={"configurable": {"thread_id": "last-model"}},
        context=context("tools:read"),
    )
    assert len(BudgetModel.observed) == 3
    assert result["finishing"]
    assert not BudgetModel.observed[-1]
    assert sum(e.decision == "executed" for e in audit.records()) == (0 if reject_batches else 4)


def test_new_turn_can_use_tools_after_previous_turn_finished():
    """同一原生线程上的新 Turn 不继承上一轮的收尾开关。"""
    components, _ = components_with_tools()
    profile = components.default_agent_profile.model_copy(update={"max_model_calls": 2})
    graph = components.agent_factory.build(profile, model=BudgetModel(batches=(1,)))
    cfg = {"configurable": {"thread_id": "two-turns"}}
    for turn in ("first", "second"):
        BudgetModel.observed = []
        result = graph.invoke(
            {"messages": [{"role": "user", "content": turn}]},
            config=cfg,
            context=context("tools:read", turn_id=turn),
        )
        assert BudgetModel.observed[0] and not BudgetModel.observed[-1]
        assert result["finish_turn_id"] == f"turn-{turn}"
