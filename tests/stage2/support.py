"""Stage 2 E2E / fault-injection tests 的确定性测试组件。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    InvocationContext,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
)
from harness_events import InMemoryEventBus
from harness_execution import BasicScheduler, ExecutionEngine
from harness_planning import PlanValidator
from harness_policy import AllowAllPolicy, Policy, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import CapabilityInvoker, DefaultInvocationContextFactory, InvocationLifecycle
from harness_spi import PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_state import InMemoryStateStore, StateStore
from harness_trace import InMemoryTracer

OutcomeFactory = Callable[[int, ToolRequest, InvocationContext], ResultEnvelope]
ScriptedOutcome = ResultEnvelope | BaseException | OutcomeFactory


class ScriptedTool(ToolSPI):
    """按调用序号返回预设结果/异常，适合 Retry 和故障注入。"""

    def __init__(
        self,
        capability_id: str,
        outcomes: Sequence[ScriptedOutcome],
        *,
        profile: CapabilityExecutionProfile | None = None,
    ) -> None:
        if not outcomes:
            raise ValueError("outcomes must not be empty")
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=profile or CapabilityExecutionProfile(),
        )
        self._outcomes = tuple(outcomes)
        self.calls = 0
        self.arguments: list[dict[str, Any]] = []
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        self.contexts.append(context)
        arguments = request.model_dump(mode="json")["arguments"]
        self.arguments.append(arguments)
        index = min(self.calls - 1, len(self._outcomes) - 1)
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(self.calls, request, context)
        return outcome


class EchoTool(ToolSPI):
    """把 Tool arguments 原样返回，便于验证跨节点 Binding。"""

    def __init__(
        self,
        capability_id: str,
        *,
        profile: CapabilityExecutionProfile | None = None,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=profile or CapabilityExecutionProfile(),
        )
        self.calls = 0
        self.arguments: list[dict[str, Any]] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        arguments = request.model_dump(mode="json")["arguments"]
        self.arguments.append(arguments)
        return ResultEnvelope.success(ResultOutput(type="json", data=arguments))


class BlockingTool(ToolSPI):
    """直到测试释放 ``release`` 前一直运行，用于 running cancellation/crash。"""

    def __init__(
        self,
        capability_id: str,
        *,
        profile: CapabilityExecutionProfile | None = None,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=profile or CapabilityExecutionProfile(),
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        arguments = request.model_dump(mode="json")["arguments"]
        return ResultEnvelope.success(ResultOutput(type="json", data=arguments))


class SleepingTool(ToolSPI):
    """执行指定时长，用于 Node timeout。"""

    def __init__(self, capability_id: str, delay_seconds: float) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.delay_seconds = delay_seconds
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        await asyncio.sleep(self.delay_seconds)
        return ResultEnvelope.success(ResultOutput(type="json", data={"done": True}))


class InvalidResultTool(ToolSPI):
    """故意违反 SPI 返回类型，用于验证 Invoker 最后防线。"""

    def __init__(self, capability_id: str) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(  # type: ignore[override]
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> object:
        self.calls += 1
        return {"not": "a ResultEnvelope"}


class TestPlugin(PluginSPI):
    """把测试 Tool 通过真实 LocalPluginLoader 接入 HarnessApplication。"""

    def __init__(self, plugin_id: str, capabilities: Sequence[ToolSPI]) -> None:
        if not capabilities:
            raise ValueError("capabilities must not be empty")
        self._plugin_id = plugin_id
        self._capabilities = tuple(capabilities)
        self.initialized = False
        self.stopped = False

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id=self._plugin_id,
            name=self._plugin_id,
            version="1.0.0",
            sdk_version="1.0.0",
            capabilities=tuple(item.descriptor().id for item in self._capabilities),
        )

    def capabilities(self) -> tuple[ToolSPI, ...]:
        return self._capabilities

    async def initialize(self) -> None:
        self.initialized = True
        self.stopped = False

    async def shutdown(self) -> None:
        self.stopped = True


@dataclass(slots=True)
class EngineFixture:
    engine: ExecutionEngine
    scheduler: BasicScheduler
    tracer: InMemoryTracer
    state_store: StateStore
    event_bus: InMemoryEventBus


def make_engine(
    *providers: ToolSPI,
    state_store: StateStore | None = None,
    policies: Sequence[Policy] | None = None,
) -> EngineFixture:
    """组装不经过 Bootstrap 的轻量执行夹具，便于精确故障注入。"""

    registry = InMemoryCapabilityRegistry()
    for provider in providers:
        registry.register(provider, plugin_id="stage2-reliability-tests")
    tracer = InMemoryTracer()
    lifecycle = InvocationLifecycle(
        tracer,
        context_factory=DefaultInvocationContextFactory(),
    )
    policy_engine = PolicyEngine(tuple(policies) if policies is not None else (AllowAllPolicy(),))
    invoker = CapabilityInvoker(
        registry,
        policy_engine,
        tracer,
        lifecycle=lifecycle,
    )
    catalog = RegistryCapabilityCatalog(registry)
    scheduler = BasicScheduler(
        invoker,
        tracer,
        lifecycle,
        capability_catalog=catalog,
    )
    store = state_store or InMemoryStateStore()
    bus = InMemoryEventBus()
    engine = ExecutionEngine(
        PlanValidator(catalog),
        scheduler,
        invoker,
        tracer,
        lifecycle,
        state_store=store,
        event_publisher=bus,
    )
    return EngineFixture(
        engine=engine,
        scheduler=scheduler,
        tracer=tracer,
        state_store=store,
        event_bus=bus,
    )


def make_request(request_id: str = "stage2-reliability-request") -> Request:
    return Request(
        request_id=request_id,
        input=RequestInput(type="json", content={"seed": 7, "message": "hello"}),
    )
