"""根 Agent 在调用预算内收尾；计数继续由原生中间件与 SQL 维护。"""

import asyncio

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from financeclaw.agent_server.context.state import ConversationState
from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.shared.turns.types import ExecutionConflict


class FinishMiddleware(AgentMiddleware):
    """预留根最后一次模型调用，超预算批次补齐回执后转入回答。"""

    state_schema = ConversationState

    def __init__(self, profile, execution=None):
        """使用发布中的限额和已有持久预算，未配置的子 Agent 不挂载。"""
        self.profile = profile
        self.execution = execution

    def _remaining(self, state, runtime):
        """本地已完成计数与 SQL 真实尝试额度分别计算。"""
        models = self.profile.max_model_calls - state.get("run_model_call_count", 0)
        tools = self.profile.max_tool_calls - state.get("run_tool_call_count", {}).get("__all__", 0)
        if self.execution is not None:
            context = trusted_context(runtime)
            self.execution.verify_context(context)
            root = self.execution.get(context.turn_id)
            models = min(models, root["release_snapshot"]["limits"]["model"] - root["model_calls"])
            tools = min(tools, root["release_snapshot"]["limits"]["tool"] - root["tool_calls"])
        return models, tools

    def before_model(self, state, runtime):
        """在最后一个本地模型名额或工具名额用完时固定收尾状态。"""
        models, tools = self._remaining(state, runtime)
        turn_id = trusted_context(runtime).turn_id
        finishing = state.get("finish_turn_id") == turn_id and state.get("finishing", False)
        return {"finish_turn_id": turn_id, "finishing": finishing or models <= 1 or tools <= 0}

    async def abefore_model(self, state, runtime):
        """异步节点通过线程读取持久预算。"""
        return await asyncio.to_thread(self.before_model, state, runtime)

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        """限流器已计数；将未执行的整批调用补成回执，不执行半批。"""
        messages = state["messages"]
        index = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)),
            None,
        )
        if index is None:
            return None
        calls = messages[index].tool_calls
        if not calls:
            return None
        answered = {m.tool_call_id for m in messages[index + 1 :] if isinstance(m, ToolMessage)}
        pending = [call for call in calls if call["id"] not in answered]
        if not pending and state.get("structured_response") is not None:
            return None
        if state.get("finishing"):
            raise ExecutionConflict("provider requested tools during the final answer attempt")
        # 原生计数包含本批尝试；SQL 计数此时尚未包含真正的工具执行。
        over_local = (
            state.get("run_tool_call_count", {}).get("__all__", 0) > self.profile.max_tool_calls
        )
        over_tree = False
        if self.execution is not None:
            root = self.execution.get(trusted_context(runtime).turn_id)
            over_tree = (
                root["tool_calls"] + len(pending) > root["release_snapshot"]["limits"]["tool"]
            )
        if not over_local and not over_tree:
            return None
        return {
            "finishing": True,
            "messages": [
                ToolMessage(
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                    content="This call was not executed because the remaining task budget cannot "
                    "fit this batch. Finish using the results already read; state any gaps.",
                )
                for call in pending
            ],
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        """沿用相同批次处理规则。"""
        return await asyncio.to_thread(self.after_model, state, runtime)

    def _prepare(self, request):
        """实际请求移除业务工具，保持应用的最终输出格式。"""
        if not request.state.get("finishing"):
            return request
        original = request.system_message.content if request.system_message else ""
        instruction = (
            "\nThe task has reached its final answer allowance. No further tools or delegation "
            "are available. Answer now using only evidence already read in this task. "
            "Explain incomplete coverage or missing facts plainly; do not invent results, "
            "treat a preview as exhaustive, or expose internal tool names, file references, "
            "budget counters or errors. Follow the required final response format."
        )
        content = (
            original + instruction
            if isinstance(original, str)
            else [*original, {"type": "text", "text": instruction}]
        )
        return request.override(
            tools=[], tool_choice=None, system_message=SystemMessage(content=content)
        )

    def wrap_model_call(self, request, handler):
        """最终请求仍经过重试内预算消费和 Manifest 记录。"""
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        """异步模型请求采用同样的有界收尾行为。"""
        return await handler(self._prepare(request))
