"""Harness 应用与本地插件生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from harness_context import ContextPipeline
from harness_contracts import (
    ApprovalDecision,
    ExecutionMode,
    ExecutionPlan,
    Request,
    RequestError,
    ResultEnvelope,
)
from harness_events import EventPublisher
from harness_execution import BasicScheduler, ExecutionEngine
from harness_memory import MemoryGateway, MemoryProvider
from harness_model import ModelGateway
from harness_planning import (
    PlanMaterializer,
    PlannerOutputNormalizer,
    PlannerRegistry,
    PlanValidator,
)
from harness_plugin_local import LoadedPlugin, LocalPluginLoader
from harness_policy import PolicyEngine
from harness_registry import CapabilityCatalog, CapabilityRegistry
from harness_routing import RequestProjector, RouteDecisionValidator, Router
from harness_runtime import (
    CapabilityInvoker,
    HarnessRuntime,
    InvocationContextFactory,
    InvocationLifecycle,
)
from harness_selection import ProviderSelector
from harness_state import StateStore
from harness_trace import Tracer

from .coordinator import RequestCoordinator, normalize_request_mode


class BootstrapState(StrEnum):
    """Composition Root 的最小生命周期状态。"""

    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"


class BootstrapStateError(RuntimeError):
    """Bootstrap 生命周期被非法使用时抛出的异常。"""


@dataclass(frozen=True, slots=True)
class HarnessComponents:
    """一次 Bootstrap 组装完成后的组件快照。"""

    registry: CapabilityRegistry
    policy_engine: PolicyEngine
    context_pipeline: ContextPipeline
    memory_provider: MemoryProvider | None
    memory_gateway: MemoryGateway | None
    tracer: Tracer
    provider_selector: ProviderSelector
    plugin_loader: LocalPluginLoader
    context_factory: InvocationContextFactory
    capability_catalog: CapabilityCatalog
    plan_validator: PlanValidator
    lifecycle: InvocationLifecycle
    invoker: CapabilityInvoker
    model_gateway: ModelGateway
    runtime: HarnessRuntime
    scheduler: BasicScheduler
    execution_engine: ExecutionEngine
    state_store: StateStore
    event_publisher: EventPublisher
    router: Router
    request_projector: RequestProjector
    route_decision_validator: RouteDecisionValidator
    request_coordinator: RequestCoordinator
    planner_registry: PlannerRegistry
    planner_output_normalizer: PlannerOutputNormalizer
    plan_materializer: PlanMaterializer


class HarnessApplication:
    """持有已组装 Harness，并协调本地插件的启动与关闭。"""

    def __init__(self, components: HarnessComponents) -> None:
        if not isinstance(components, HarnessComponents):
            raise TypeError("components must be HarnessComponents")
        self._components = components
        self._state = BootstrapState.CREATED
        self._lock = asyncio.Lock()

    @property
    def components(self) -> HarnessComponents:
        return self._components

    @property
    def state(self) -> BootstrapState:
        return self._state

    @property
    def runtime(self) -> HarnessRuntime:
        return self._components.runtime

    @property
    def invoker(self) -> CapabilityInvoker:
        return self._components.invoker

    @property
    def model_gateway(self) -> ModelGateway:
        return self._components.model_gateway

    @property
    def provider_selector(self) -> ProviderSelector:
        return self._components.provider_selector

    @property
    def execution_engine(self) -> ExecutionEngine:
        return self._components.execution_engine

    @property
    def scheduler(self) -> BasicScheduler:
        return self._components.scheduler

    @property
    def state_store(self) -> StateStore:
        return self._components.state_store

    @property
    def event_publisher(self) -> EventPublisher:
        return self._components.event_publisher

    @property
    def registry(self) -> CapabilityRegistry:
        return self._components.registry

    @property
    def capability_catalog(self) -> CapabilityCatalog:
        return self._components.capability_catalog

    @property
    def plan_validator(self) -> PlanValidator:
        return self._components.plan_validator

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._components.policy_engine

    @property
    def context_pipeline(self) -> ContextPipeline:
        return self._components.context_pipeline

    @property
    def memory_provider(self) -> MemoryProvider | None:
        return self._components.memory_provider

    @property
    def memory_gateway(self) -> MemoryGateway | None:
        return self._components.memory_gateway

    @property
    def tracer(self) -> Tracer:
        return self._components.tracer

    @property
    def plugin_loader(self) -> LocalPluginLoader:
        return self._components.plugin_loader

    @property
    def router(self) -> Router:
        return self._components.router

    @property
    def request_projector(self) -> RequestProjector:
        return self._components.request_projector

    @property
    def route_decision_validator(self) -> RouteDecisionValidator:
        return self._components.route_decision_validator

    @property
    def request_coordinator(self) -> RequestCoordinator:
        return self._components.request_coordinator

    @property
    def planner_registry(self) -> PlannerRegistry:
        return self._components.planner_registry

    @property
    def planner_output_normalizer(self) -> PlannerOutputNormalizer:
        return self._components.planner_output_normalizer

    @property
    def plan_materializer(self) -> PlanMaterializer:
        return self._components.plan_materializer

    @property
    def loaded_plugins(self) -> tuple[LoadedPlugin, ...]:
        return self._components.plugin_loader.loaded_plugins()

    async def start(self) -> tuple[LoadedPlugin, ...]:
        """发现、初始化并注册本地插件；重复 start 保持幂等。"""

        async with self._lock:
            if self._state is BootstrapState.STARTED:
                return self.loaded_plugins
            if self._state is BootstrapState.STOPPED:
                raise BootstrapStateError("stopped harness application cannot be restarted")

            loaded = await self._components.plugin_loader.load_all()
            self._state = BootstrapState.STARTED
            return loaded

    async def shutdown(self) -> None:
        """关闭所有已加载插件；重复 shutdown 保持幂等。"""

        async with self._lock:
            if self._state is BootstrapState.STOPPED:
                return
            if self._state is BootstrapState.CREATED:
                self._state = BootstrapState.STOPPED
                return

            try:
                await self._components.plugin_loader.shutdown()
            finally:
                self._state = BootstrapState.STOPPED

    async def invoke(self, request: Request) -> ResultEnvelope:
        """仅在应用启动后把请求交给已组装的 ``HarnessRuntime``。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before invoke")
        return await self._components.runtime.invoke(request)

    async def handle(
        self,
        request: Request,
        mode: ExecutionMode | str | None = None,
    ) -> ResultEnvelope:
        """归一化执行模式并把请求交给统一 RequestCoordinator。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before handle")
        try:
            normalized_request = normalize_request_mode(request, mode)
        except RequestError as exc:
            return ResultEnvelope.failure(exc.to_detail())
        return await self._components.request_coordinator.handle(normalized_request)

    async def execute_plan(
        self,
        request: Request,
        plan: ExecutionPlan,
    ) -> ResultEnvelope:
        """仅在应用启动后验证并推进一个 ExecutionPlan。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before execute_plan")
        return await self._components.execution_engine.execute(request, plan)

    async def resume_plan(self, plan_id: str) -> ResultEnvelope:
        """仅在应用启动后从 StateStore 恢复并继续同一个 plan_id。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before resume_plan")
        return await self._components.execution_engine.resume(plan_id)

    async def resolve_approval(
        self,
        plan_id: str,
        decision: ApprovalDecision,
    ) -> ResultEnvelope:
        """持久化显式审批决定，并继续推进同一个 Plan。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before resolve_approval")
        return await self._components.execution_engine.resolve_approval(plan_id, decision)

    async def complete_async_node(
        self,
        plan_id: str,
        node_id: str,
        terminal_result: ResultEnvelope,
    ) -> ResultEnvelope:
        """提交异步 Capability 的终态结果，并继续推进同一个 Plan。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError(
                "harness application must be started before complete_async_node"
            )
        return await self._components.execution_engine.complete_async_node(
            plan_id,
            node_id,
            terminal_result,
        )

    async def cancel_plan(self, plan_id: str, reason: str | None = None) -> bool:
        """请求取消当前进程内由 ExecutionEngine 推进的活动 Plan。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before cancel_plan")
        return await self._components.execution_engine.cancel(plan_id, reason)

    async def __aenter__(self) -> HarnessApplication:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.shutdown()
