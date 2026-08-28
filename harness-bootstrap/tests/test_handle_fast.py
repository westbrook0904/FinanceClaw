"""Stage 3B Step 4 统一 handle() FAST 路径测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_bootstrap import BootstrapStateError, build_harness
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ExecutionMode,
    InvocationContext,
    Request,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    RouteDecision,
    RouteSource,
    RouteType,
)
from harness_policy import Policy, PolicyContext, PolicyDecision, PolicyPhase
from harness_routing import Router, RoutingContext, SafeRequestProjector
from harness_runtime import InvocationContextFactory
from harness_spi import PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_trace import InMemoryTracer, SpanStatus, SpanType


class RecordingTool(ToolSPI):
    def __init__(self, *, capability_id: str = "echo.tool/v1", name: str = "echo") -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.name = name
        self.contexts: list[InvocationContext] = []

    @property
    def calls(self) -> int:
        return len(self.contexts)

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.contexts.append(context)
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data={
                    "provider": self.name,
                    **request.model_dump(mode="json")["arguments"],
                },
            )
        )


class StubPlugin(PluginSPI):
    def __init__(self, plugin_id: str, providers: tuple[ToolSPI, ...]) -> None:
        self.plugin_id = plugin_id
        self.providers = providers

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id=self.plugin_id,
            name=self.plugin_id,
            version="1.0.0",
            sdk_version="1",
            capabilities=tuple(provider.descriptor().id for provider in self.providers),
        )

    def capabilities(self) -> tuple[ToolSPI, ...]:
        return self.providers

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class RecordingRouter(Router):
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision
        self.calls: list[RoutingContext] = []

    @property
    def router_id(self) -> str:
        return "recording-router"

    async def route(self, context: RoutingContext) -> RouteDecision:
        self.calls.append(context)
        return self.decision


class DenyPreRoutePolicy(Policy):
    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_ROUTE})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.deny(self.name, reason="blocked before routing")


class RecordingContextFactory(InvocationContextFactory):
    def __init__(self, deadline_at: datetime) -> None:
        self.deadline_at = deadline_at
        self.requests: list[Request] = []

    def create(self, request: Request) -> InvocationContext:
        self.requests.append(request)
        return InvocationContext(request=request, deadline_at=self.deadline_at)


def make_request(
    *,
    plugin_id: str | None = None,
    mode: ExecutionMode = ExecutionMode.AUTO,
    trace: bool = True,
) -> Request:
    return Request(
        input=RequestInput(type="json", content={"message": "hello"}),
        target=RequestTarget(capability="echo.tool/v1", plugin=plugin_id),
        options=RequestOptions(trace=trace, execution_mode=mode),
    )


def fast_decision(*, metadata: dict | None = None) -> RouteDecision:
    return RouteDecision(
        mode=ExecutionMode.FAST,
        route_type=RouteType.DIRECT_CAPABILITY,
        source=RouteSource.RULE,
        capability_id="echo.tool/v1",
        confidence=1.0,
        reason_code="TEST_FAST",
        metadata=metadata or {},
    )


class HandleFastTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_explicit_target_dispatches_fast_with_route_metadata(self) -> None:
        tool = RecordingTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(make_request())

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["message"], "hello")
        self.assertEqual(
            dict(result.metadata),
            {
                "execution_mode": "fast",
                "route_type": "direct_capability",
                "route_reason_code": "EXPLICIT_TARGET",
                "router_id": "rule-router",
                "capability_id": "echo.tool/v1",
            },
        )
        self.assertEqual(tool.calls, 1)
        await app.shutdown()

    async def test_fast_handle_matches_invoke_business_result(self) -> None:
        tool = RecordingTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            entry_point_group=None,
        )
        await app.start()
        request = make_request(trace=False)

        direct = await app.invoke(request)
        handled = await app.handle(request, ExecutionMode.FAST)

        self.assertEqual(handled.status, direct.status)
        self.assertEqual(handled.output, direct.output)
        self.assertEqual(tool.calls, 2)
        await app.shutdown()

    async def test_handle_creates_one_context_and_one_request_span(self) -> None:
        deadline = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
        context_factory = RecordingContextFactory(deadline)
        tracer = InMemoryTracer()
        tool = RecordingTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            context_factory=context_factory,
            tracer=tracer,
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(make_request(), mode="fast")

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(len(context_factory.requests), 1)
        self.assertEqual(context_factory.requests[0].options.execution_mode, ExecutionMode.FAST)
        self.assertEqual(tool.contexts[0].deadline_at, deadline)
        spans = tracer.spans(trace_id=result.trace_id)
        request_spans = [span for span in spans if span.type is SpanType.REQUEST]
        handle_spans = [span for span in spans if span.name == "runtime.handle"]
        self.assertEqual(len(request_spans), 1)
        self.assertEqual(len(handle_spans), 1)
        self.assertEqual(handle_spans[0].parent_span_id, request_spans[0].span_id)
        self.assertTrue(all(span.status is not SpanStatus.RUNNING for span in spans))
        await app.shutdown()

    async def test_mode_sugar_copies_request_and_conflicts_fail_before_context(self) -> None:
        deadline = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
        context_factory = RecordingContextFactory(deadline)
        tool = RecordingTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            context_factory=context_factory,
            entry_point_group=None,
        )
        await app.start()
        auto_request = make_request()

        success = await app.handle(auto_request, ExecutionMode.FAST)
        conflicting_request = make_request(mode=ExecutionMode.FAST)
        conflict = await app.handle(conflicting_request, ExecutionMode.PLAN)

        self.assertEqual(success.status, ResultStatus.SUCCESS)
        self.assertEqual(auto_request.options.execution_mode, ExecutionMode.AUTO)
        self.assertEqual(conflict.status, ResultStatus.FAILED)
        self.assertEqual(conflict.error.code, "HARNESS.REQUEST.MODE_CONFLICT")
        self.assertEqual(len(context_factory.requests), 1)
        await app.shutdown()

    async def test_pre_route_deny_stops_router_and_tool(self) -> None:
        router = RecordingRouter(fast_decision())
        tool = RecordingTool()
        app = build_harness(
            plugins=(StubPlugin("echo-plugin", (tool,)),),
            policies=(DenyPreRoutePolicy(),),
            router=router,
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(make_request())

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(result.error.code, "HARNESS.POLICY.DENIED")
        self.assertEqual(router.calls, [])
        self.assertEqual(tool.calls, 0)
        await app.shutdown()

    async def test_router_metadata_cannot_create_provider_or_plugin_pin(self) -> None:
        first = RecordingTool(name="first")
        second = RecordingTool(name="second")
        router = RecordingRouter(
            fast_decision(
                metadata={
                    "plugin_id": "missing-plugin",
                    "provider_id": "missing-provider",
                }
            )
        )
        app = build_harness(
            plugins=(
                StubPlugin("first-plugin", (first,)),
                StubPlugin("second-plugin", (second,)),
            ),
            router=router,
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(make_request(plugin_id="second-plugin"))

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["provider"], "second")
        self.assertEqual(first.calls, 0)
        self.assertEqual(second.calls, 1)
        self.assertNotIn("plugin_id", result.metadata)
        self.assertNotIn("provider_id", result.metadata)
        await app.shutdown()

    async def test_factory_wires_custom_router_and_projector_by_identity(self) -> None:
        router = RecordingRouter(fast_decision())
        projector = SafeRequestProjector(metadata_allowlist=("safe",))
        app = build_harness(
            router=router,
            request_projector=projector,
            entry_point_group=None,
        )

        self.assertIs(app.router, router)
        self.assertIs(app.request_projector, projector)
        self.assertIs(app.request_coordinator.router, router)
        self.assertIs(app.request_coordinator.request_projector, projector)
        self.assertIs(app.request_coordinator.lifecycle, app.components.lifecycle)
        self.assertIs(app.request_coordinator.invoker, app.invoker)

    async def test_handle_requires_started_application(self) -> None:
        app = build_harness(entry_point_group=None)

        with self.assertRaises(BootstrapStateError):
            await app.handle(make_request())

        await app.start()
        await app.shutdown()
        with self.assertRaises(BootstrapStateError):
            await app.handle(make_request())


if __name__ == "__main__":
    unittest.main()
