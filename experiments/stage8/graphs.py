"""真实 Agent Server 探针图：仅使用合成输入，无模型、工具副作用或真实身份。"""

import asyncio
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from financeclaw.kernel.delegation.models import AgentHandoffV2, DelegationResult


class ProbeState(TypedDict, total=False):
    """无敏感数据的探针输入与检查点输出。"""

    task_id: str
    request_id: str
    turn_id: str
    conversation_id: str
    output: dict[str, Any]
    messages: list[dict[str, Any]]


def parent_node(state: ProbeState) -> dict[str, Any]:
    """稳定 handoff 在真实 checkpoint 暂停，结果经原 DelegationResult 校验。"""
    handoff = AgentHandoffV2(
        handoff_id=state["request_id"],
        parent_run_id=state["task_id"],
        parent_turn_id=state["turn_id"],
        conversation_id=state["conversation_id"],
        agent_id="probe_child",
        target_version="1.0.0",
        task="synthetic stage8 probe",
        arguments={"scope": "synthetic"},
    )
    result = DelegationResult.model_validate(interrupt(handoff.model_dump(mode="json")))
    if result.delegation_id != handoff.handoff_id or result.parent_run_id != state["task_id"]:
        raise ValueError("probe result bound to another request")
    return {
        "output": {"status": "completed", "child": result.output},
        "messages": [
            {
                "type": "tool",
                "tool_call_id": state["request_id"],
                "content": result.model_dump_json(),
            }
        ],
    }


def child_node(state: ProbeState) -> dict[str, Any]:
    """子任务有独立用户交互；必须显式回答后才能完成。"""
    answer = interrupt(
        {
            "kind": "interaction",
            "request_id": state["request_id"],
            "point_id": "probe_scope",
            "question": "Confirm synthetic scope",
        }
    )
    if answer != {"scope": "synthetic"}:
        raise ValueError("invalid probe interaction answer")
    return {"output": {"scope": "synthetic", "answer_applied": True}}


def success_node(state: ProbeState) -> dict[str, Any]:
    """简单成功用于核对 run 身份和回调。"""
    return {"output": {"task_id": state["task_id"]}}


def failure_node(state: ProbeState) -> dict[str, Any]:
    """预期错误用于验证 error 回调。"""
    raise ValueError("intentional stage8 synthetic failure")


async def slow_node(state: ProbeState) -> dict[str, Any]:
    """留出真实取消窗口，不调用外部系统。"""
    await asyncio.sleep(4)
    return {"output": {"task_id": state["task_id"]}}


def graph(node):
    """构造单节点探针，检查点由真实 Agent Server 提供。"""
    builder = StateGraph(ProbeState)
    builder.add_node("probe", node)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    return builder.compile()


parent = graph(parent_node)
child = graph(child_node)
success = graph(success_node)
failure = graph(failure_node)
slow = graph(slow_node)
