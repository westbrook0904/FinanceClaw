"""分离原生节点边界与请求投影，确保准备、恢复和真实模型尝试顺序一致。"""

import asyncio

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command

from financeclaw.agent_server.context.planning import insert_skill_messages
from financeclaw.agent_server.context.state import ConversationState
from financeclaw.kernel.skills import SkillError
from financeclaw.shared.skills.access import ACCESS_KEY, RESOURCE_KEY, merge_access


class SkillBoundaryMiddleware(AgentMiddleware):
    """每轮模型循环按可信身份重置或复验引用，先移除失效派生内容。"""

    state_schema = ConversationState

    def __init__(self, service):
        """仅保存固定的领域服务。"""
        self.service = service

    def before_model(self, state, runtime):
        """恢复不依赖 before_agent；旧状态不能自报另一个身份取得激活。"""
        binding = self.service.binding(runtime, state)
        candidate = {**state, "skill_state": binding}
        return {"skill_state": binding, **self.service.sanitize(runtime, candidate)}

    async def abefore_model(self, state, runtime):
        """身份及授权 SQL 读取在线程执行。"""
        return await asyncio.to_thread(self.before_model, state, runtime)


class SkillInitializationMiddleware(AgentMiddleware):
    """显式选择在首轮回答前通过完整候选准备写入原生 state。"""

    def __init__(self, service):
        """与模型加载工具共享同一激活领域入口。"""
        self.service = service

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        """只在候选成功后标记初始化，进度事件不构成持久化凭据。"""
        if state["skill_state"].get("explicit_initialization_done"):
            return None
        selected = self.service.selections(runtime, state)
        if not selected:
            return {"skill_state": {**state["skill_state"], "explicit_initialization_done": True}}
        ref = selected[0]
        self.service.emit(runtime, state, ref, "preparing")
        try:
            update = self.service.activate(runtime, state, ref["skill_id"], explicit=True)
        except SkillError as exc:
            self.service.emit(runtime, state, ref, "failed")
            return {
                **getattr(exc, "state_update", {}),
                "skill_state": {**state["skill_state"], "preparation_error": exc.payload()},
                "jump_to": "end",
            }
        self.service.emit(runtime, state, ref, "prepared")
        return update

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        """候选摘要及同步存储操作不阻塞事件循环。"""
        return await asyncio.to_thread(self.before_model, state, runtime)


class SkillProjectionMiddleware(AgentMiddleware):
    """只投影带来源正文，并把实际输入的访问依赖传递给模型输出。"""

    def __init__(self, service):
        """保留固定发布服务，激活集合每次从 request.state 读取。"""
        self.service = service

    def project(self, request):
        """与预算共享顺序：历史摘要、旧轮次、技能正文、本轮输入及完整工具批次。"""
        directory, bodies, metadata = self.service.projection(request.state)
        existing = request.system_message or SystemMessage(content="")
        content = existing.content
        system = SystemMessage(
            content=(
                content + directory
                if isinstance(content, str)
                else [*content, {"type": "text", "text": directory}]
            ),
            additional_kwargs={**existing.additional_kwargs, **metadata},
        )
        messages = insert_skill_messages(
            request.messages,
            bodies,
            user_message_id=request.state.get("skill_state", {}).get("user_message_id"),
        )
        return request.override(system_message=system, messages=messages)

    def wrap_model_call(self, request, handler):
        """请求包装只负责投影，输出来源由每次实际调用的最终中间件签发。"""
        return handler(self.project(request))

    async def awrap_model_call(self, request, handler):
        """异步调用使用相同纯渲染。"""
        return await handler(self.project(request))

    def _validate_tool(self, request):
        """审批恢复或派发前，复验模型入参继承的受限来源。"""
        self.service.validate_active(request.runtime, request.state)
        return self.service.validate_request(
            request.runtime, request.state, request.state["messages"]
        )

    def _result(self, request, result, inherited):
        """非平台工具的标签不能成为可信来源，所有真实工具结果继承当前约束。"""
        if isinstance(result, Command) and isinstance(result.update, dict):
            update = dict(result.update)
            update["messages"] = [
                self._result(request, m, inherited) for m in update.get("messages", [])
            ]
            return Command(
                graph=result.graph, update=update, resume=result.resume, goto=result.goto
            )
        if not isinstance(result, ToolMessage) or result.tool_call_id != request.tool_call["id"]:
            return result
        metadata = dict(result.additional_kwargs)
        trusted = (request.tool.metadata or {}).get("skill_tool") or (
            request.tool.metadata or {}
        ).get("skill_provenance")
        refs = metadata.get(ACCESS_KEY, []) if trusted else []
        if not trusted:
            metadata.pop(RESOURCE_KEY, None)
        metadata[ACCESS_KEY] = merge_access(inherited, refs)
        return result.model_copy(update={"additional_kwargs": metadata})

    def wrap_tool_call(self, request, handler):
        """在归档内层绑定来源，让首份工件也受同样约束。"""
        refs = self._validate_tool(request)
        return self._result(request, handler(request), refs)

    async def awrap_tool_call(self, request, handler):
        """异步工具授权读取离开事件循环。"""
        refs = await asyncio.to_thread(self._validate_tool, request)
        return self._result(request, await handler(request), refs)
