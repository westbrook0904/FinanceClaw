"""FinanceClaw 核心应用与本地插件生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from harness_context import ContextPipeline
from harness_contracts import Request, ResultEnvelope
from harness_events import EventPublisher
from harness_memory import MemoryGateway, MemoryProvider
from harness_plugin_local import LoadedPlugin, LocalPluginLoader
from harness_policy import PolicyEngine
from harness_registry import CapabilityCatalog, CapabilityRegistry
from harness_runtime import (
    CapabilityInvoker,
    HarnessRuntime,
    InvocationContextFactory,
    InvocationLifecycle,
)
from harness_selection import ProviderSelector
from harness_trace import Tracer


class BootstrapState(StrEnum):
    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"


class BootstrapStateError(RuntimeError):
    """Bootstrap 生命周期被非法使用时抛出的异常。"""


@dataclass(frozen=True, slots=True)
class HarnessComponents:
    """Composition Root 组装完成后的核心组件快照。"""

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
    lifecycle: InvocationLifecycle
    invoker: CapabilityInvoker
    runtime: HarnessRuntime
    event_publisher: EventPublisher


class HarnessApplication:
    """持有已组装的 FinanceClaw 核心，并管理本地插件生命周期。"""

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
    def provider_selector(self) -> ProviderSelector:
        return self._components.provider_selector

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
    def loaded_plugins(self) -> tuple[LoadedPlugin, ...]:
        return self._components.plugin_loader.loaded_plugins()

    async def start(self) -> tuple[LoadedPlugin, ...]:
        async with self._lock:
            if self._state is BootstrapState.STARTED:
                return self.loaded_plugins
            if self._state is BootstrapState.STOPPED:
                raise BootstrapStateError("stopped harness application cannot be restarted")
            loaded = await self._components.plugin_loader.load_all()
            self._state = BootstrapState.STARTED
            return loaded

    async def shutdown(self) -> None:
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
        """执行显式单 Capability 调用；Agent/Workflow 将复用同一个 invoker。"""

        if self._state is not BootstrapState.STARTED:
            raise BootstrapStateError("harness application must be started before invoke")
        return await self._components.runtime.invoke(request)

    async def __aenter__(self) -> HarnessApplication:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.shutdown()
