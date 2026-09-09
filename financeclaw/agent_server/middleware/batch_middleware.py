"""模型产出与原生 HITL／ToolNode 之间的批次准入，不接管工具调度。"""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from financeclaw.agent_server.agents.directives import parse_invocation_directive
from financeclaw.agent_server.middleware.middleware import _context
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.policy import ToolDecisionType, ToolPolicy
from financeclaw.kernel.tools import SideEffect
from financeclaw.shared.execution_ledger.repository import ExecutionConflict


class ToolBatchMiddleware(AgentMiddleware):
    """整个批次先准入；拒绝时补齐每个 call_id 并让模型重新决策。

    create_agent 的 after_model 按中间件声明的逆序执行。本中间件必须放在
    HITL 之后，从而在审批中断和工具派发之前执行；真实图回归测试固定此约束。
    """

    def __init__(
        self, catalog: ToolCatalog, policy: ToolPolicy, *, max_batch: int, execution: Any = None
    ) -> None:
        """使用同一发布目录，并在已拒绝动作后封闭本根任务的副作用。"""
        self.catalog = catalog
        self.policy = policy
        self.max_batch = max_batch
        self.execution = execution

    def _reason(self, calls: list[dict[str, Any]], state: Any, runtime: Any) -> str | None:
        """整批判定先于 HITL，单调用也不能绕过已持久化的用户拒绝。"""
        identifiers = [call.get("id") for call in calls]
        if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(calls):
            # 无法产生无歧义 ToolMessage 映射时受控停止，不能执行一个损坏批次。
            raise ExecutionConflict("tool batch has missing or duplicate call IDs")
        if len(calls) > self.max_batch:
            return "tool batch exceeds the configured limit"
        context = _context(runtime.context)
        if self.execution is not None and context.root_run_id is not None:
            root = self.execution.get(context.root_run_id or context.run_id)
            if root["side_effects_denied"]:
                for call in calls:
                    try:
                        managed = self.catalog.resolve(call["name"])
                    except LookupError:
                        continue
                    if managed.governance.side_effect is not SideEffect.READ:
                        return (
                            "the user rejected this task's side effects; "
                            "new authorization is required"
                        )
        if len(calls) <= 1:
            return None
        latest_user = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
        )
        if latest_user and isinstance(latest_user.content, str):
            if parse_invocation_directive(latest_user.content) is not None:
                return "an explicit directive permits exactly one invocation"
        context = _context(runtime.context)
        for call in calls:
            try:
                managed = self.catalog.resolve(call["name"])
            except LookupError:
                return "multi-call batches must contain registered independent READ tools"
            arguments = call.get("args", {})
            if not isinstance(arguments, Mapping):
                return "tool arguments must be objects"
            decision = self.policy.evaluate(context, managed.governance, dict(arguments))
            if (
                managed.governance.side_effect is not SideEffect.READ
                or decision.effect is ToolDecisionType.REQUIRE_APPROVAL
            ):
                return "composites, writes and approvals must occupy an exclusive batch"
        return None

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """未准入的任何调用都不执行，保留 AIMessage 与结果消息完整对应。"""
        message = state["messages"][-1]
        if not isinstance(message, AIMessage) or not message.tool_calls:
            return None
        reason = self._reason(message.tool_calls, state, runtime)
        if reason is None:
            return None
        return {
            "messages": [
                ToolMessage(
                    content=json.dumps({"error": "unsupported_tool_batch", "reason": reason}),
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                )
                for call in message.tool_calls
            ],
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """异步入口复用同一准入规则。"""
        return await asyncio.to_thread(self.after_model, state, runtime)
