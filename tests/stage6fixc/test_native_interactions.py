"""纯原生图证明已完成节点不重算，审批与资料回复不能混用。"""

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from financeclaw.modules.execution.repository import digest
from financeclaw.modules.interactions import InteractionPoint
from financeclaw.orchestration.tools.interaction import request_user_interaction


class State(TypedDict, total=False):
    """进度由原生检查点持久化，交互恢复不创建新业务执行。"""

    progress: str
    answer: dict
    approved: bool


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_native_input_and_declared_approval_preserve_previous_work(asynchronous):
    """真实 StateGraph 的 input 与 approval 原位恢复，先前节点只执行一次。"""
    calls = []
    input_point = InteractionPoint(
        point_id="parameters",
        kind="input",
        question="对象？",
        response_schema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    )
    approval = InteractionPoint(
        point_id="publish", kind="approval", question="发布？", required_scope="reports:approve"
    )

    def work(state):
        """已完成的昂贵步骤不能在每次回答时重跑。"""
        calls.append("work")
        return {"progress": "saved"}

    def ask(state):
        """问题函数之后的回答存入当前图状态。"""
        return {"answer": request_user_interaction(input_point)["answer"]}

    def confirm(state):
        """动作的对象取真实已回答资料，并绑定这一份快照。"""
        action = {"publish": state["answer"]["symbol"], "progress": state["progress"]}
        response = request_user_interaction(approval, action=action)
        return {"approved": response["decision"] == "approve"}

    graph = (
        StateGraph(State).add_node("work", work).add_node("ask", ask).add_node("confirm", confirm)
    )
    graph.add_edge(START, "work").add_edge("work", "ask").add_edge("ask", "confirm").add_edge(
        "confirm", END
    )
    compiled = graph.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "native"}}

    async def invoke(value):
        """同步和异步执行共享一组语义断言。"""
        return (
            await compiled.ainvoke(value, config)
            if asynchronous
            else compiled.invoke(value, config)
        )

    first = (await invoke({}))["__interrupt__"][0]
    second = (
        await invoke(Command(resume={first.id: {"kind": "input", "answer": {"symbol": "AAPL"}}}))
    )["__interrupt__"][0]
    action = second.value["action"]
    result = await invoke(
        Command(
            resume={
                second.id: {
                    "kind": "approval",
                    "decision": "approve",
                    "action_hash": digest(action),
                }
            }
        )
    )
    assert result["approved"] and result["answer"] == {"symbol": "AAPL"}
    assert calls == ["work"] and first.id != second.id
