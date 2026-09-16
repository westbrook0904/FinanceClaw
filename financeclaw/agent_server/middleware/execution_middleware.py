"""根执行累计预算与共享资源门控，原生重试仍由 LangChain 执行。"""

import asyncio
from collections.abc import Callable
from threading import BoundedSemaphore
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.agent_server.middleware.middleware import _context
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.subgraph_scope import verify_graph_release
from financeclaw.kernel.tools import SideEffect
from financeclaw.shared.turns.budget import TurnExecutionRepository
from financeclaw.shared.turns.types import ExecutionBudgetExceeded, ExecutionConflict


class ExecutionBudgetMiddleware(AgentMiddleware):
    """每个模型／工具真实尝试计费，resume 和 retry 不能重置根预算。

    资源门控由同一 Factory 创建的 Agent 共享，限制其工具 I/O 并发；
    部署总上限需结合 Factory／worker 实例数配置，不能把进程内信号量当成分布式锁。
    """

    def __init__(
        self,
        repository: TurnExecutionRepository | None,
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
        if self.repository is None:
            if self.profile is not None and (
                self.profile.worker_manifest or self.profile.context_policy == "worker-task-only-v1"
            ):
                raise ExecutionConflict("product releases require a persistent Turn")
            return  # Explicitly constructed standalone leaf tests have no business repository.
        self.repository.verify_context(context)
        if self.profile is not None:
            if self.profile.context_policy == "worker-task-only-v1":
                verify_graph_release(self.repository, context, self.profile)
            elif self.repository.get(context.turn_id)["release_snapshot"].get(
                "profile"
            ) != self.profile.model_dump(mode="json"):
                raise ExecutionConflict("executing graph release differs from the pinned profile")
        root = self.repository.get(context.turn_id)
        if kind == "tool" and root["side_effects_denied"]:
            managed = self.catalog.resolve(request.tool_call["name"])
            reentering = (
                managed.governance.side_effect is SideEffect.COMPOSITE
                and self.repository.rejected_invocation(context, request.tool_call.get("id"))
            )
            if managed.governance.side_effect is not SideEffect.READ and not reentering:
                raise ExecutionConflict("user rejected side effects")
        final_answer = bool(
            kind == "model"
            and self.profile is not None
            and self.profile.finish_on_budget
            and self.profile.agent_id == root["release_snapshot"]["profile"].get("agent_id")
            and request.state.get("finishing")
            and request.state.get("finish_turn_id") == context.turn_id
        )
        self.repository.consume(context.turn_id, kind, final_answer=final_answer)

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
            try:
                self._consume(request, "tool")
                return handler(request)
            except ExecutionBudgetExceeded as error:
                return self._budget_receipt(request, error)

    def _budget_receipt(self, request, error):
        """根把子执行额度耗尽作为有界失败回执，其他异常继续传播。"""
        if self.profile is None or not self.profile.finish_on_budget:
            raise error
        context = _context(request.runtime.context)
        if self.repository is not None:
            self.repository.verify_context(context)
        call = request.tool_call
        return ToolMessage(
            name=call["name"],
            tool_call_id=call["id"],
            status="error",
            content="The delegated work could not finish within the remaining task budget. "
            "Use only previously confirmed results and state what remains unverified.",
        )

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """可取消的门控等待，不遗留在线程池中永远占用资源的 acquire。"""
        call = getattr(request, "tool_call", {})
        if (
            call.get("name")
            and self.catalog.resolve(call["name"]).governance.side_effect is SideEffect.COMPOSITE
        ):
            try:
                await asyncio.to_thread(self._consume, request, "tool")
                return await handler(request)
            except ExecutionBudgetExceeded as error:
                return await asyncio.to_thread(self._budget_receipt, request, error)
        while not self.gate.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            await asyncio.to_thread(self._consume, request, "tool")
            return await handler(request)
        except ExecutionBudgetExceeded as error:
            return await asyncio.to_thread(self._budget_receipt, request, error)
        finally:
            self.gate.release()
