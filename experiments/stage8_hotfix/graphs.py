"""HF-0：真实 ReAct／Tool／子图，仅模型决策和业务数据使用确定性合成实现。

本模块不加载 FinanceClaw bootstrap、SDK、凭据或数据库。只有根图对外注册，
Worker 图在装配期构建，并通过原生 ToolRuntime 的 config/context 内部调用。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

ROOT_GRAPH_ID = "hf0_orchestrator_v1"
SCENARIOS = (
    "serial",
    "question",
    "two_questions",
    "workflow_approve",
    "workflow_reject",
    "hitl_approve",
    "hitl_reject",
    "repeat_agent",
    "repeat_workflow",
)


@dataclass(frozen=True)
class ProbeContext:
    """合成的可信运行上下文；Worker 继承根身份，绑定原工具调用。"""

    root_id: str
    tool_call_id: str = ""
    worker: str = "root"


class Recorder:
    """记录实际执行次数与原生命名空间；持久文件仅用于探针证据。"""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def record(self, event: str, *, root_id: str, **fields: Any) -> None:
        """原子追加一条合成记录；不保存环境、凭据或用户内容。"""
        row = {"event": event, "root_id": root_id, **fields}
        with self.lock:
            self.events.append(row)
            if self.path:
                with self.path.open("a") as output:
                    output.write(json.dumps(row, sort_keys=True) + "\n")


def scenario_calls(scenario: str) -> list[dict[str, Any]]:
    """模型固定逐轮选择工具；不在应用层调度、派发或恢复子任务。"""
    research = {"name": "research", "mode": "plain", "label": "first"}
    workflow = {"name": "review", "mode": "plain", "label": "second"}
    plans = {
        "serial": [research, workflow],
        "question": [{**research, "mode": "question"}],
        "two_questions": [{**research, "mode": "two_questions"}],
        "workflow_approve": [{**workflow, "mode": "approval"}],
        "workflow_reject": [{**workflow, "mode": "approval"}],
        "hitl_approve": [{"name": "approved_agent", "mode": "hitl", "label": "first"}],
        "hitl_reject": [{"name": "approved_agent", "mode": "hitl", "label": "first"}],
        "repeat_agent": [research, {**research, "mode": "question", "label": "second"}],
        "repeat_workflow": [workflow, {**workflow, "mode": "approval", "label": "second"}],
    }
    return plans[scenario]


class ProbeModel(BaseChatModel):
    """按已收到的 ToolMessage 决定下一步，无计数器驱动，也不调用远端模型。"""

    role: str
    recorder: Any
    bound_names: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        """标识合成模型，避免误称真实 LLM 能力验收。"""
        return "hf0-synthetic-" + self.role

    def bind_tools(self, tools, **kwargs):
        """保留共享计数器，绑定真实工具 schema 的名称。"""
        return self.model_copy(
            update={
                "bound_names": tuple(t["name"] if isinstance(t, dict) else t.name for t in tools)
            }
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """主 Agent 逐轮调用两个子图；Worker 自己通过工具完成有界任务。"""
        request = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        results = [m for m in messages if isinstance(m, ToolMessage)]
        self.recorder.record(
            "model",
            root_id=request["root_id"],
            role=self.role,
            label=request.get("label", "root"),
            result_count=len(results),
            human_count=sum(isinstance(m, HumanMessage) for m in messages),
        )
        if self.role == "root":
            calls = scenario_calls(request["scenario"])
            if len(results) < len(calls):
                planned = calls[len(results)]
                answer = self._call(
                    planned["name"],
                    f"root-call-{len(results) + 1}",
                    mode=planned["mode"],
                    label=planned["label"],
                )
            else:
                answer = AIMessage(
                    content=json.dumps(
                        {
                            "kind": "root_final",
                            "results": [json.loads(m.content) for m in results],
                        }
                    )
                )
        else:
            count = 2 if request["mode"] == "two_questions" else 1
            if len(results) < count:
                name = (
                    "commit_marker"
                    if self.role == "hitl"
                    else "ask_scope"
                    if request["mode"] in {"question", "two_questions"}
                    else "read_marker"
                )
                answer = self._call(name, f"leaf-call-{len(results) + 1}", label=request["label"])
            else:
                answer = AIMessage(
                    content=json.dumps(
                        {
                            "kind": "worker_result",
                            "label": request["label"],
                            "answers": [m.content for m in results],
                        }
                    )
                )
        return ChatResult(generations=[ChatGeneration(message=answer)])

    def _call(self, name: str, identifier: str, **arguments: Any) -> AIMessage:
        """使用 LangChain 标准 tool_calls，确保对应 ToolNode 真正执行。"""
        if name not in self.bound_names:
            raise ValueError("probe model selected an unbound tool")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "id": identifier,
                    "args": arguments,
                    "type": "tool_call",
                }
            ],
        )


class ReviewState(TypedDict, total=False):
    """与父 Agent messages 不共享的 Workflow 状态。"""

    label: str
    mode: str
    prepared: str
    decision: str
    output: dict[str, Any]


def build_graph(*, recorder: Recorder | None = None, checkpointer=None):
    """装配两个 Agent Worker、一个 Workflow Worker，最后创建唯一根 ReAct。"""
    recorder = recorder or Recorder()

    def event(name, runtime, **fields):
        """从运行时取得身份与命名空间，不让模型提供根归属。"""
        context = runtime.context
        config = getattr(runtime, "config", {})
        recorder.record(
            name,
            root_id=context.root_id,
            root_call_id=context.tool_call_id,
            worker=context.worker,
            namespace=config.get("configurable", {}).get("checkpoint_ns"),
            **fields,
        )

    @tool
    async def read_marker(label: str, runtime: ToolRuntime[ProbeContext]) -> str:
        """Read a synthetic marker without accessing an external system."""
        event("read", runtime, label=label)
        return "read:" + label

    @tool
    async def ask_scope(label: str, runtime: ToolRuntime[ProbeContext]) -> str:
        """Ask for a synthetic scope within the current Worker graph."""
        payload = {
            "kind": "user_interaction",
            "point_id": "synthetic_scope",
            "root_call_id": runtime.context.tool_call_id,
            "leaf_call_id": runtime.tool_call_id,
            "label": label,
            "worker_release": "hf0_research@1",
            "revision": 1,
            "deadline": "2099-01-01T00:00:00Z",
        }
        event("question.enter", runtime, label=label, leaf_call_id=runtime.tool_call_id)
        answer = interrupt(payload)
        if answer != {"scope": "synthetic"}:
            raise ValueError("synthetic scope answer did not match")
        event("question.applied", runtime, label=label, leaf_call_id=runtime.tool_call_id)
        return "scope:synthetic:" + label

    @tool
    async def commit_marker(label: str, runtime: ToolRuntime[ProbeContext]) -> str:
        """Commit a synthetic marker only after native human approval."""
        event("effect", runtime, label=label)
        return "committed:" + label

    research_agent = create_agent(
        ProbeModel(role="research", recorder=recorder),
        [read_marker, ask_scope],
        context_schema=ProbeContext,
        checkpointer=None,
        name="hf0_research",
    )
    approved_agent = create_agent(
        ProbeModel(role="hitl", recorder=recorder),
        [commit_marker],
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "commit_marker": {"allowed_decisions": ["approve", "reject"]},
                }
            )
        ],
        context_schema=ProbeContext,
        checkpointer=None,
        name="hf0_approved_agent",
    )

    async def prepare(state: ReviewState, runtime: Runtime[ProbeContext]):
        """审批前已完成节点不应随顶层 resume 重跑。"""
        event("workflow.prepare", runtime, label=state["label"])
        return {"prepared": "prepared:" + state["label"]}

    async def approve(state: ReviewState, runtime: Runtime[ProbeContext]):
        """显式 Workflow interrupt；节点重入可观察且无副作用。"""
        event("workflow.approval.enter", runtime, label=state["label"])
        if state["mode"] != "approval":
            return {"decision": "not_required"}
        decision = interrupt(
            {
                "kind": "workflow_approval",
                "point_id": "synthetic_review",
                "root_call_id": runtime.context.tool_call_id,
                "worker_release": "hf0_review@1",
                "label": state["label"],
                "action": {"name": "synthetic_marker", "label": state["label"]},
                "revision": 1,
                "deadline": "2099-01-01T00:00:00Z",
            }
        )
        if decision not in {"approve", "reject"}:
            raise ValueError("invalid workflow decision")
        return {"decision": decision}

    async def finish(state: ReviewState, runtime: Runtime[ProbeContext]):
        """只记录已批准的合成副作用；正常结果直接返回父 Tool。"""
        event("workflow.finish", runtime, label=state["label"])
        if state["decision"] == "approve":
            event("effect", runtime, label=state["label"])
        return {
            "output": {
                "kind": "workflow_result",
                "label": state["label"],
                "prepared": state["prepared"],
                "decision": state["decision"],
            }
        }

    builder = StateGraph(ReviewState, context_schema=ProbeContext)
    builder.add_node("prepare", prepare)
    builder.add_node("approve", approve)
    builder.add_node("finish", finish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approve")
    builder.add_edge("approve", "finish")
    builder.add_edge("finish", END)
    review_graph = builder.compile(checkpointer=None, name="hf0_review")

    async def invoke_agent(graph, worker, mode, label, runtime):
        """传播原 config，子图自己的原生命名空间由 LangGraph 分配。"""
        event("tool.enter", runtime, label=label, tool_name=worker, call_id=runtime.tool_call_id)
        result = await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=json.dumps(
                            {
                                "root_id": runtime.context.root_id,
                                "mode": mode,
                                "label": label,
                            }
                        )
                    )
                ]
            },
            config=runtime.config,
            context=replace(runtime.context, tool_call_id=runtime.tool_call_id, worker=worker),
        )
        event("tool.return", runtime, label=label, tool_name=worker, call_id=runtime.tool_call_id)
        return result["messages"][-1].content

    @tool
    async def research(mode: str, label: str, runtime: ToolRuntime[ProbeContext]) -> str:
        """Call an internal research Agent and return its bounded public result."""
        return await invoke_agent(research_agent, "research", mode, label, runtime)

    @tool
    async def approved_agent_tool(mode: str, label: str, runtime: ToolRuntime[ProbeContext]) -> str:
        """Call an internal Agent with native HITL before its synthetic write."""
        return await invoke_agent(approved_agent, "approved_agent", mode, label, runtime)

    approved_agent_tool.name = "approved_agent"

    @tool
    async def review(mode: str, label: str, runtime: ToolRuntime[ProbeContext]) -> str:
        """Call an internal deterministic Workflow; return without a child Run."""
        event("tool.enter", runtime, label=label, tool_name="review", call_id=runtime.tool_call_id)
        result = await review_graph.ainvoke(
            {"label": label, "mode": mode},
            config=runtime.config,
            context=replace(runtime.context, tool_call_id=runtime.tool_call_id, worker="review"),
        )
        event("tool.return", runtime, label=label, tool_name="review", call_id=runtime.tool_call_id)
        return json.dumps(result["output"])

    return create_agent(
        ProbeModel(role="root", recorder=recorder),
        [research, approved_agent_tool, review],
        context_schema=ProbeContext,
        checkpointer=checkpointer,
        name=ROOT_GRAPH_ID,
    )
