"""LLMRouter 作为 RuleRouter fallback 的 handle() 集成测试。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
)
from harness_model import (
    GenerateRequest,
    GenerateResult,
    ModelFinishReason,
    ModelGateway,
    ModelOutput,
    ModelProvider,
    ModelResponseFormat,
    ModelUsage,
)
from harness_registry import InMemoryCapabilityRegistry
from harness_routing import LLMRouter, RouteDecisionValidator, RuleRouter
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
                    "route_type": "direct_capability",
                    "source": "model",
                    "capability_id": TOOL_ID,
                    "confidence": 1.0,
                    "reason_code": "MODEL_ECHO",
                },
            ),
            ModelUsage(input_tokens=8, output_tokens=6, total_tokens=14),
            finish_reason=ModelFinishReason.STOP,
        )


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
            router=RuleRouter(fallback=llm_router),
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
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
