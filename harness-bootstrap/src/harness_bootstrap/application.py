"""阶段一 Harness 应用生命周期。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from harness_contracts import Request, ResultEnvelope
from harness_plugin_local import LoadedPlugin, LocalPluginLoader
from harness_policy import PolicyEngine
from harness_registry import CapabilityRegistry
from harness_runtime import (
    CapabilityInvoker,
    HarnessRuntime,
    InvocationContextFactory,
    InvocationLifecycle,
)
from harness_trace import Tracer


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
    tracer: Tracer
    plugin_loader: LocalPluginLoader
    context_factory: InvocationContextFactory
    lifecycle: InvocationLifecycle
    invoker: CapabilityInvoker
    runtime: HarnessRuntime


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
    def registry(self) -> CapabilityRegistry:
        return self._components.registry

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._components.policy_engine

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

    async def __aenter__(self) -> HarnessApplication:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.shutdown()
