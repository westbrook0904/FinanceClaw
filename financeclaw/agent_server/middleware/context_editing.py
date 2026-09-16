"""在原生 state 准备阶段归档完整工具批次中的旧结果，重启后保持同一投影。"""

import asyncio

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.agent_server.context.artifacts import ToolResultArchive
from financeclaw.agent_server.context.planning import completed_tool_batches, projected_messages
from financeclaw.agent_server.context.state import ConversationState
from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.shared.artifacts.repository import ArtifactNotFound
from financeclaw.shared.llm.budget import ContextBudgetPlanner


class ToolContextEditingMiddleware(AgentMiddleware):
    """归档成功后返回原生 reducer 更新，禁止只修改模型请求副本。"""

    state_schema = ConversationState

    def __init__(
        self,
        service,
        budget,
        *,
        planner=None,
        system_prompt="",
        tools=(),
        output_schema=None,
        skill_projection=None,
    ):
        """与摘要及最终检查共享完整请求预算与固定 Schema。"""
        self.archive = ToolResultArchive(service)
        self.budget = budget
        self.planner = planner or ContextBudgetPlanner.from_model(None, budget)
        self.counter = self.planner.counter
        self.system_prompt = system_prompt
        self.tools = tools
        self.output_schema = output_schema
        self.skill_projection = skill_projection

    def before_model(self, state, runtime):
        """只投影已完整返回的批次；未完成/需保护结果保持原始消息。"""
        previous = state.get("context_privacy_epoch")
        current = state.get("memory_privacy_epoch", previous)
        if state.get("memory_forget_requested") or (previous is not None and previous != current):
            return None
        messages = state["messages"]
        batches = completed_tool_batches(messages)
        if batches is None:
            return None
        tokens = self.planner.estimate(
            projected_messages(
                state, system_prompt=self.system_prompt, skill_projection=self.skill_projection
            ),
            tools=self.tools,
            output_schema=self.output_schema,
        )
        if tokens < min(self.budget.soft_input_tokens, self.planner.input_limit):
            return None
        results = [
            messages[index]
            for batch in batches
            for index in batch[1:]
            if isinstance(messages[index], ToolMessage)
        ]
        eligible = (
            results[: -self.budget.tool_results_to_keep]
            if self.budget.tool_results_to_keep
            else results
        )
        updates = []
        for message in eligible:
            if message.additional_kwargs.get("preserve_structure") or (
                message.name or ""
            ).startswith("request_user__"):
                continue
            if message.additional_kwargs.get("privacy_invalidated"):
                continue
            try:
                projected = self.archive.project(message, trusted_context(runtime))
            except ArtifactNotFound:
                if message.additional_kwargs.get("memory_derived"):
                    return None
                raise
            if not message.id:
                raise ValueError("native state tool messages must have stable IDs before editing")
            # Never replace a small result by a larger reference envelope.
            if self.counter.message(projected) < self.counter.message(message):
                updates.append(projected)
        return (
            {"messages": updates, "context_compaction_reason": "artifact_offloaded"}
            if updates
            else None
        )

    async def abefore_model(self, state, runtime):
        """工件 I/O 在线程执行，更新由框架写入原生 checkpoint。"""
        return await asyncio.to_thread(self.before_model, state, runtime)
