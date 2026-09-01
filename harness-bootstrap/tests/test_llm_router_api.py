"""LLMRouter 作为 RuleRouter fallback 的 handle() 集成测试。"""

from __future__ import annotations

import json
import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    ModelProviderFeatures,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    StructuredOutputSpec,
)
from harness_events import ExecutionEventName
from harness_model import (
    GenerateRequest,
    GenerateResult,
    ModelFinishReason,
    ModelGateway,
    ModelOutput,
    ModelProvider,
    ModelResponseFormat,
    ModelUsage,
    PreparedStructuredOutput,
)
from harness_model.schema import structured_schema_hash
from harness_registry import InMemoryCapabilityRegistry
from harness_routing import (
    LLMRouter,
    RouteDecisionValidator,
    RoutingPipeline,
    RuleRouter,
)
from harness_spi import Capability, PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_trace import InMemoryTracer, SpanType

MODEL_ID = "model.route/v1"
TOOL_ID = "echo.dynamic/v1"


class RouteModel(ModelProvider):
    def __init__(self) -> None:
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=MODEL_ID,
            name="Route model",
            type=CapabilityType.MODEL,
            version="1.0.0",
        )

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        self.calls += 1
        return GenerateResult.success(
            ModelOutput(
                type=ModelResponseFormat.JSON,
                data={
                    "mode": "fast",
                    "capability_id": TOOL_ID,
                    "confidence": 1.0,
                    "reason_code": "MODEL_ECHO",
                },
            ),
            ModelUsage(input_tokens=8, output_tokens=6, total_tokens=14),
            finish_reason=ModelFinishReason.STOP,
        )

    @property
    def features(self) -> ModelProviderFeatures:
        return ModelProviderFeatures(
            json_object=True,
            json_schema=True,
            json_schema_strict=True,
        )

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput:
        return PreparedStructuredOutput(
            provider_id=f"route-models:{MODEL_ID}",
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
        )

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        return await self.generate(request, context)


class EchoTool(ToolSPI):
    def __init__(self) -> None:
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=TOOL_ID,
            name="Dynamic echo",
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data=request.model_dump(mode="json")["arguments"],
            )
        )


class ToolPlugin(PluginSPI):
    def __init__(self, tool: EchoTool) -> None:
        self.tool = tool

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="dynamic-tools",
            name="Dynamic tools",
            version="1.0.0",
            sdk_version="1",
            capabilities=(TOOL_ID,),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return (self.tool,)

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class LLMRouterHandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_llm_router_gets_its_own_route_span(self) -> None:
        registry = InMemoryCapabilityRegistry()
        tracer = InMemoryTracer()
        model = RouteModel()
        tool = EchoTool()
        registry.register(model, plugin_id="route-models")
        llm_router = LLMRouter(
            ModelGateway(registry, tracer),
            route_model_capability_id=MODEL_ID,
            decision_validator=RouteDecisionValidator(),
        )
        app = build_harness(
            registry=registry,
            tracer=tracer,
            router=llm_router,
            plugins=(ToolPlugin(tool),),
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(
            Request(input=RequestInput(type="dynamic-goal", content={"message": "hello"}))
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        route_span = next(
            span for span in tracer.spans(trace_id=result.trace_id) if span.type is SpanType.ROUTE
        )
        self.assertEqual(route_span.attributes["router_id"], "llm-router")
        self.assertEqual(route_span.attributes["decision_source"], "model")
        await app.shutdown()

    async def test_unmatched_request_routes_then_invokes_with_one_trace(self) -> None:
        registry = InMemoryCapabilityRegistry()
        tracer = InMemoryTracer()
        model = RouteModel()
        tool = EchoTool()
        registry.register(model, plugin_id="route-models")
        llm_router = LLMRouter(
            ModelGateway(registry, tracer),
            route_model_capability_id=MODEL_ID,
            decision_validator=RouteDecisionValidator(),
        )
        app = build_harness(
            registry=registry,
            tracer=tracer,
            router=RoutingPipeline(RuleRouter(), llm_router),
            plugins=(ToolPlugin(tool),),
            entry_point_group=None,
        )
        await app.start()

        result = await app.handle(
            Request(input=RequestInput(type="dynamic-goal", content={"message": "hello"}))
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data["message"], "hello")
        self.assertEqual(result.metadata["route_reason_code"], "MODEL_ECHO")
        self.assertEqual(model.calls, 1)
        self.assertEqual(tool.calls, 1)
        spans = tracer.spans(trace_id=result.trace_id)
        self.assertEqual(sum(span.type is SpanType.REQUEST for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.MODEL for span in spans), 1)
        self.assertEqual(sum(span.type is SpanType.TOOL for span in spans), 1)
        route_span = next(span for span in spans if span.type is SpanType.ROUTE)
        model_span = next(span for span in spans if span.type is SpanType.MODEL)
        parents = {span.span_id: span.parent_span_id for span in spans}
        ancestor_id = model_span.parent_span_id
        while ancestor_id is not None and ancestor_id != route_span.span_id:
            ancestor_id = parents.get(ancestor_id)
        self.assertEqual(ancestor_id, route_span.span_id)
        self.assertEqual(route_span.attributes["router_id"], "routing-pipeline")
        self.assertEqual(route_span.attributes["requested_mode"], "auto")
        self.assertEqual(route_span.attributes["effective_mode"], "auto")
        self.assertEqual(route_span.attributes["route_type"], "direct_capability")
        self.assertEqual(route_span.attributes["decision_source"], "model")
        self.assertEqual(route_span.attributes["reason_code"], "MODEL_ECHO")
        self.assertEqual(route_span.attributes["confidence"], 1.0)
        self.assertEqual(len(route_span.attributes["catalog_snapshot_hash"]), 64)
        self.assertEqual(len(route_span.attributes["request_summary_hash"]), 64)

        events = app.event_publisher.events()
        event_names = [event.name for event in events]
        self.assertIn(ExecutionEventName.ROUTE_DECIDED, event_names)
        self.assertIn(ExecutionEventName.MODE_SELECTED, event_names)
        route_event = next(
            event for event in events if event.name is ExecutionEventName.ROUTE_DECIDED
        )
        self.assertEqual(route_event.trace_id, result.trace_id)
        serialized_observability = json.dumps(
            {
                "spans": [span.model_dump(mode="json") for span in spans],
                "events": [event.model_dump(mode="json") for event in events],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn("hello", serialized_observability)
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
