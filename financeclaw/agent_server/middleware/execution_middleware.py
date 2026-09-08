"""根任务树累计预算与共享资源门控，原生重试仍由 LangChain 执行。"""

import asyncio
from collections.abc import Callable
from threading import BoundedSemaphore
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from financeclaw.agent_server.middleware.middleware import _context
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.delegation import DelegationTool, delegation_handoff_id
from financeclaw.kernel.tools import SideEffect
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, ExecutionRepository


class ExecutionBudgetMiddleware(AgentMiddleware):
    """每个模型／工具真实尝试计费，resume 和 retry 不能重置根预算。

    资源门控由同一 Factory 创建的 Agent 共享，限制其工具 I/O 并发；
    部署总上限需结合 Factory／worker 实例数配置，不能把进程内信号量当成分布式锁。
    """

    def __init__(
        self,
        repository: ExecutionRepository | None,
        catalog: ToolCatalog,
        gate: BoundedSemaphore,
        profile: Any = None,
    ) -> None:
        """共享同一业务预算仓储和 Factory 资源门。"""
        self.repository = repository
        self.catalog = catalog
        self.gate = gate
        self.profile = profile

    def _consume(self, request: Any, kind: str) -> None:
        """无持久化 root ID 的纯图测试仍受框架限额，产品运行必须有预算快照。"""
        context = _context(request.runtime.context)
        if context.root_run_id is None:
            return
        if self.repository is None:
            raise ExecutionConflict("persistent execution budget is not configured")
        self.repository.verify_context(context)
        execution = self.repository.get(context.run_id)
        if self.profile is not None and execution["snapshot"].get(
            "profile"
        ) != self.profile.model_dump(mode="json"):
            raise ExecutionConflict("executing graph release differs from the pinned profile")
        root = self.repository.get(context.root_run_id)
        if kind == "tool" and root["side_effects_denied"]:
            managed = self.catalog.resolve(request.tool_call["name"])
            delivering = isinstance(
                managed.tool, DelegationTool
            ) and self.repository.delivery_in_progress(
                context.run_id,
                delegation_handoff_id(
                    parent_run_id=context.run_id,
                    tool_call_id=request.tool_call["id"],
                    kind=managed.tool.handoff_kind,
                    target_id=managed.tool.target_id,
                ),
            )
            if managed.governance.side_effect is not SideEffect.READ and not delivering:
                raise ExecutionConflict("user rejected side effects; re-delegation is not allowed")
        self.repository.consume(context.run_id, kind)

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        """在重试内部计入每次模型请求。"""
        self._consume(request, "model")
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        """异步模型预算检查不阻塞事件循环。"""
        await asyncio.to_thread(self._consume, request, "model")
        return await handler(request)

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """同步工具在共享资源门内执行；interrupt 也必须释放资源。"""
        with self.gate:
            self._consume(request, "tool")
            return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """可取消的门控等待，不遗留在线程池中永远占用资源的 acquire。"""
        while not self.gate.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            await asyncio.to_thread(self._consume, request, "tool")
            return await handler(request)
        finally:
            self.gate.release()
