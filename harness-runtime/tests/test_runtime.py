"""harness-runtime 的阶段一 Invocation 行为测试。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCategory,
    HarnessTimeoutError,
    IdentityContext,
    InvocationContext,
    Request,
    RequestInput,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    TenantContext,
    TraceContext,
)
from harness_policy import AllowAllPolicy, Policy, PolicyContext, PolicyDecision, PolicyEngine
from harness_registry import InMemoryCapabilityRegistry
from harness_runtime import (
    DefaultInvocationContextFactory,
    HarnessRuntime,
    InvocationContextFactory,
)
from harness_spi import AgentRequest, AgentSPI, ToolRequest, ToolSPI
from harness_trace import InMemoryTracer, SpanStatus, SpanType


class IdSequence:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"


def make_tracer() -> InMemoryTracer:
    return InMemoryTracer(
        trace_id_factory=IdSequence("trace"),
        span_id_factory=IdSequence("span"),
    )


def make_request(
    capability: str,
    *,
    content: object = None,
    plugin: str | None = None,
    timeout_ms: int | None = None,
    trace: bool = True,
) -> Request:
    return Request(
        request_id="req-001",
        input=RequestInput(
            type="json",
            content={} if content is None else content,
        ),
        target=RequestTarget(capability=capability, plugin=plugin),
        options={"timeout_ms": timeout_ms, "trace": trace},
    )


class RecordingAgent(AgentSPI):
    def __init__(
        self,
        capability_id: str = "echo.reply/v1",
        *,
        result: ResultEnvelope | None = None,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.AGENT,
            version="1.0.0",
        )
        self.result = result
        self.calls = 0
        self.last_request: AgentRequest | None = None
        self.last_context: InvocationContext | None = None

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        self.last_request = request
        self.last_context = context
        return self.result or ResultEnvelope.success(
            ResultOutput(type=request.input.type, data=request.input.content)
        )


class RecordingTool(ToolSPI):
    def __init__(
        self,
        capability_id: str = "math.add/v1",
        *,
        descriptor_type: CapabilityType = CapabilityType.TOOL,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=descriptor_type,
            version="1.0.0",
        )
        self.calls = 0
        self.last_request: ToolRequest | None = None
        self.last_context: InvocationContext | None = None

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        self.last_request = request
        self.last_context = context
        return ResultEnvelope.success(ResultOutput(type="number", data=3))


class FailingAgent(RecordingAgent):
    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        raise RuntimeError("provider exploded")


class TimeoutAgent(RecordingAgent):
    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        await asyncio.sleep(0.05)
        return await super().invoke(request, context)


class BlockingAgent(RecordingAgent):
    def __init__(self) -> None:
        super().__init__("blocking.agent/v1")
        self.started = asyncio.Event()

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class InvalidResultAgent(RecordingAgent):
    async def invoke(  # type: ignore[override]
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> object:
        return {"unexpected": True}


class DenyPolicy(Policy):
    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.deny(self.name, reason="blocked by test policy")


class ExplodingPolicy(Policy):
    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        raise ValueError("policy exploded")


class TrustedContextFactory(InvocationContextFactory):
    def __init__(self, *, incoming_trace: TraceContext | None = None) -> None:
        self.incoming_trace = incoming_trace

    def create(self, request: Request) -> InvocationContext:
        return InvocationContext(
            request=request,
            identity=IdentityContext(subject="trusted-user", scopes={"invoke"}),
            tenant=TenantContext(tenant_id="tenant-a"),
            trace_context=self.incoming_trace,
        )


def make_runtime(
    provider: AgentSPI | ToolSPI,
    *,
    policy_engine: PolicyEngine | None = None,
    tracer: InMemoryTracer | None = None,
    plugin_id: str = "test-plugin",
    context_factory: InvocationContextFactory | None = None,
) -> tuple[HarnessRuntime, InMemoryCapabilityRegistry, InMemoryTracer]:
    registry = InMemoryCapabilityRegistry()
    registry.register(provider, plugin_id=plugin_id)
    effective_tracer = tracer or make_tracer()
    runtime = HarnessRuntime(
        registry,
        policy_engine or PolicyEngine((AllowAllPolicy(),)),
        effective_tracer,
        context_factory=context_factory,
    )
    return runtime, registry, effective_tracer


class ContextFactoryTests(unittest.TestCase):
    def test_default_factory_sets_deadline_without_trusting_request_identity(self) -> None:
        request = Request(
            tenant_id="untrusted-tenant",
            user_id="untrusted-user",
            input=RequestInput(type="json", content={}),
            target=RequestTarget(capability="echo.reply/v1"),
            options={"timeout_ms": 250},
        )
        now = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)

        context = DefaultInvocationContextFactory(clock=lambda: now).create(request)

        self.assertIsNone(context.identity)
        self.assertIsNone(context.tenant)
        self.assertEqual(
            context.deadline_at,
            datetime(2026, 8, 25, 6, 0, 0, 250000, tzinfo=UTC),
        )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_success_runs_full_phase_one_pipeline(self) -> None:
        agent = RecordingAgent()
        runtime, _, tracer = make_runtime(agent)

        result = await runtime.invoke(make_request("echo.reply/v1", content={"text": "hi"}))

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["text"], "hi")
        self.assertEqual(agent.calls, 1)
        self.assertEqual(result.trace_id, "trace-1")
        self.assertIsNotNone(agent.last_context.trace_context)

        spans = tracer.spans(trace_id=result.trace_id)
        self.assertEqual(
            [span.type for span in spans],
            [
                SpanType.REQUEST,
                SpanType.RUNTIME,
                SpanType.REGISTRY_RESOLVE,
                SpanType.POLICY,
                SpanType.CAPABILITY,
                SpanType.AGENT,
            ],
        )
        request_span, runtime_span, _, _, capability_span, agent_span = spans
        self.assertEqual(runtime_span.parent_span_id, request_span.span_id)
        self.assertEqual(capability_span.parent_span_id, runtime_span.span_id)
        self.assertEqual(agent_span.parent_span_id, capability_span.span_id)
        self.assertEqual(agent.last_context.trace_context.span_id, agent_span.span_id)
        self.assertTrue(all(span.status is SpanStatus.OK for span in spans))

    async def test_tool_receives_request_content_as_arguments(self) -> None:
        tool = RecordingTool()
        runtime, _, _ = make_runtime(tool)

        result = await runtime.invoke(
            make_request("math.add/v1", content={"left": 1, "right": 2})
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(
            tool.last_request.model_dump(mode="json")["arguments"],
            {"left": 1, "right": 2},
        )

    async def test_tool_rejects_non_object_input_before_provider_execution(self) -> None:
        tool = RecordingTool()
        runtime, _, tracer = make_runtime(tool)

        result = await runtime.invoke(make_request("math.add/v1", content="1 + 2"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.REQUEST)
        self.assertEqual(tool.calls, 0)
        leaf = next(span for span in tracer.spans() if span.type is SpanType.TOOL)
        self.assertEqual(leaf.status, SpanStatus.ERROR)

    async def test_target_plugin_is_used_during_resolution(self) -> None:
        agent = RecordingAgent()
        runtime, _, _ = make_runtime(agent, plugin_id="echo-plugin")

        allowed = await runtime.invoke(
            make_request("echo.reply/v1", plugin="echo-plugin")
        )
        missing = await runtime.invoke(
            make_request("echo.reply/v1", plugin="another-plugin")
        )

        self.assertEqual(allowed.status, ResultStatus.SUCCESS)
        self.assertEqual(missing.status, ResultStatus.FAILED)
        self.assertEqual(missing.error.category, ErrorCategory.REGISTRY)

    async def test_policy_deny_returns_denied_without_invoking_provider(self) -> None:
        agent = RecordingAgent()
        runtime, _, tracer = make_runtime(
            agent,
            policy_engine=PolicyEngine((DenyPolicy(),)),
        )

        result = await runtime.invoke(make_request("echo.reply/v1"))

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(result.error.category, ErrorCategory.POLICY)
        self.assertEqual(agent.calls, 0)
        self.assertFalse(any(span.type is SpanType.CAPABILITY for span in tracer.spans()))
        policy_span = next(span for span in tracer.spans() if span.type is SpanType.POLICY)
        self.assertEqual(policy_span.status, SpanStatus.OK)
        self.assertEqual(policy_span.attributes["effect"], "deny")

    async def test_policy_exception_is_normalized_as_policy_failure(self) -> None:
        agent = RecordingAgent()
        runtime, _, tracer = make_runtime(
            agent,
            policy_engine=PolicyEngine((ExplodingPolicy(),)),
        )

        result = await runtime.invoke(make_request("echo.reply/v1"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.POLICY)
        policy_span = next(span for span in tracer.spans() if span.type is SpanType.POLICY)
        self.assertEqual(policy_span.status, SpanStatus.ERROR)

    async def test_registry_miss_is_normalized_as_registry_failure(self) -> None:
        agent = RecordingAgent()
        runtime, registry, tracer = make_runtime(agent)
        registry.unregister("echo.reply/v1", plugin_id="test-plugin")

        result = await runtime.invoke(make_request("echo.reply/v1"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.REGISTRY)
        resolve_span = next(
            span for span in tracer.spans() if span.type is SpanType.REGISTRY_RESOLVE
        )
        self.assertEqual(resolve_span.status, SpanStatus.ERROR)

    async def test_provider_exception_is_wrapped_as_capability_failure(self) -> None:
        agent = FailingAgent()
        runtime, _, tracer = make_runtime(agent)

        result = await runtime.invoke(make_request("echo.reply/v1"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.CAPABILITY)
        self.assertEqual(result.error.details["cause_type"], "RuntimeError")
        agent_span = next(span for span in tracer.spans() if span.type is SpanType.AGENT)
        self.assertEqual(agent_span.status, SpanStatus.ERROR)

    async def test_provider_failed_result_marks_execution_spans_error(self) -> None:
        failure = ResultEnvelope.failure(
            HarnessTimeoutError("upstream timed out").to_detail()
        )
        agent = RecordingAgent(result=failure)
        runtime, _, tracer = make_runtime(agent)

        result = await runtime.invoke(make_request("echo.reply/v1"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        for span in tracer.spans():
            if span.type in {
                SpanType.REQUEST,
                SpanType.RUNTIME,
                SpanType.CAPABILITY,
                SpanType.AGENT,
            }:
                self.assertEqual(span.status, SpanStatus.ERROR)

    async def test_timeout_returns_timeout_failure(self) -> None:
        agent = TimeoutAgent()
        runtime, _, tracer = make_runtime(agent)

        result = await runtime.invoke(make_request("echo.reply/v1", timeout_ms=1))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.category, ErrorCategory.TIMEOUT)
        leaf = next(span for span in tracer.spans() if span.type is SpanType.AGENT)
        self.assertEqual(leaf.status, SpanStatus.ERROR)

    async def test_calling_task_cancellation_propagates_and_closes_open_spans(self) -> None:
        agent = BlockingAgent()
        runtime, _, tracer = make_runtime(agent)
        task = asyncio.create_task(runtime.invoke(make_request("blocking.agent/v1")))
        await agent.started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        statuses = {span.type: span.status for span in tracer.spans()}
        self.assertEqual(statuses[SpanType.AGENT], SpanStatus.CANCELLED)
        self.assertEqual(statuses[SpanType.CAPABILITY], SpanStatus.CANCELLED)
        self.assertEqual(statuses[SpanType.RUNTIME], SpanStatus.CANCELLED)
        self.assertEqual(statuses[SpanType.REQUEST], SpanStatus.CANCELLED)

    async def test_trace_false_skips_runtime_trace(self) -> None:
        agent = RecordingAgent()
        runtime, _, tracer = make_runtime(agent)

        result = await runtime.invoke(make_request("echo.reply/v1", trace=False))

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertIsNone(result.trace_id)
        self.assertEqual(tracer.spans(), ())
        self.assertIsNone(agent.last_context.trace_context)

    async def test_incoming_trace_context_is_parent_of_request_span(self) -> None:
        incoming = TraceContext(trace_id="external-trace", span_id="remote-parent")
        agent = RecordingAgent()
        runtime, _, tracer = make_runtime(
            agent,
            context_factory=TrustedContextFactory(incoming_trace=incoming),
        )

        result = await runtime.invoke(make_request("echo.reply/v1"))

        request_span = next(span for span in tracer.spans() if span.type is SpanType.REQUEST)
        self.assertEqual(result.trace_id, "external-trace")
        self.assertEqual(request_span.trace_id, "external-trace")
        self.assertEqual(request_span.parent_span_id, "remote-parent")

    async def test_provider_type_mismatch_is_rejected_and_traced(self) -> None:
        tool = RecordingTool(descriptor_type=CapabilityType.AGENT)
        runtime, _, tracer = make_runtime(tool)

        result = await runtime.invoke(make_request("math.add/v1"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.CAPABILITY.TYPE_MISMATCH")
        capability = next(
            span for span in tracer.spans() if span.type is SpanType.CAPABILITY
        )
        self.assertEqual(capability.status, SpanStatus.ERROR)
        self.assertFalse(any(span.type is SpanType.TOOL for span in tracer.spans()))

    async def test_invalid_provider_result_is_rejected(self) -> None:
        agent = InvalidResultAgent()
        runtime, _, _ = make_runtime(agent)

        result = await runtime.invoke(make_request("echo.reply/v1"))

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.CAPABILITY.INVALID_RESULT")


if __name__ == "__main__":
    unittest.main()
