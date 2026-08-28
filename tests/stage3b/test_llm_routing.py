"""LLMRouter structured output、失败归一化与零业务执行 Gate。"""

from __future__ import annotations

import json
import unittest

from harness_bootstrap import build_harness
from harness_contracts import ProviderError, ResultStatus
from harness_events import ExecutionEventName
from harness_model import GenerateResult, ModelGateway
from harness_registry import InMemoryCapabilityRegistry
from harness_routing import LLMRouter, RouteDecisionValidator, RuleRouter
from harness_trace import InMemoryTracer, SpanType

from .support import (
    ECHO_TOOL_ID,
    ROUTE_MODEL_ID,
    AcceptancePlugin,
    RecordingTool,
    ScriptedModel,
    make_request,
)


def route_output(*, capability_id: str = ECHO_TOOL_ID) -> dict[str, object]:
    return {
        "mode": "fast",
        "route_type": "direct_capability",
        "source": "model",
        "capability_id": capability_id,
        "confidence": 0.9,
        "reason_code": "STAGE3B_MODEL_FAST",
    }


class LLMRoutingAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        outcome: dict[str, object] | GenerateResult,
    ):
        registry = InMemoryCapabilityRegistry()
        tracer = InMemoryTracer()
        model = ScriptedModel(ROUTE_MODEL_ID, outcome)
        tool = RecordingTool()
        registry.register(model, plugin_id="stage3b-route-model")
        llm_router = LLMRouter(
            ModelGateway(registry, tracer),
            route_model_capability_id=ROUTE_MODEL_ID,
            decision_validator=RouteDecisionValidator(),
        )
        app = build_harness(
            registry=registry,
            tracer=tracer,
            router=RuleRouter(fallback=llm_router),
            plugins=(AcceptancePlugin((tool,)),),
            entry_point_group=None,
        )
        await app.start()
        result = await app.handle(make_request())
        await app.shutdown()
        return result, app, tracer, model, tool

    async def test_valid_model_decision_routes_and_executes_with_one_trace(self) -> None:
        result, app, tracer, model, tool = await self._run(route_output())

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["execution_mode"], "fast")
        self.assertEqual(result.metadata["route_reason_code"], "STAGE3B_MODEL_FAST")
        self.assertEqual(model.calls, 1)
        self.assertEqual(tool.calls, 1)

        spans = tracer.spans(trace_id=result.trace_id)
        route_span = next(span for span in spans if span.type is SpanType.ROUTE)
        model_span = next(span for span in spans if span.type is SpanType.MODEL)
        provider_span = next(span for span in spans if span.type is SpanType.TOOL)
        parents = {span.span_id: span.parent_span_id for span in spans}
        ancestor = model_span.parent_span_id
        while ancestor is not None and ancestor != route_span.span_id:
            ancestor = parents.get(ancestor)
        self.assertEqual(ancestor, route_span.span_id)
        self.assertEqual(provider_span.trace_id, route_span.trace_id)
        self.assertEqual(route_span.attributes["decision_source"], "model")
        self.assertEqual(len(route_span.attributes["catalog_snapshot_hash"]), 64)
        self.assertEqual(len(route_span.attributes["request_summary_hash"]), 64)

        event_names = [event.name for event in app.event_publisher.events()]
        self.assertIn(ExecutionEventName.ROUTE_DECIDED, event_names)
        self.assertIn(ExecutionEventName.MODE_SELECTED, event_names)
        serialized = json.dumps(
            {
                "spans": [span.model_dump(mode="json") for span in spans],
                "events": [event.model_dump(mode="json") for event in app.event_publisher.events()],
            },
            sort_keys=True,
        )
        self.assertNotIn("stage3b-secret-goal", serialized)

    async def test_invalid_model_decision_is_rejected_before_capability(self) -> None:
        result, app, _tracer, model, tool = await self._run(
            route_output(capability_id="stage3b.missing/v1")
        )

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.ROUTE.INVALID_DECISION")
        self.assertEqual(model.calls, 1)
        self.assertEqual(tool.calls, 0)
        self.assertIn(
            ExecutionEventName.ROUTE_FAILED,
            [event.name for event in app.event_publisher.events()],
        )

    async def test_model_failure_exposes_only_safe_cause_code(self) -> None:
        raw_secret = "stage3b raw provider response"
        failure = ProviderError(
            raw_secret,
            code="HARNESS.MODEL.ACCEPTANCE_FAILURE",
            details={"raw_response": raw_secret},
        )
        result, app, tracer, model, tool = await self._run(
            GenerateResult.failure(failure.to_detail())
        )

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.ROUTE.MODEL_FAILED")
        self.assertEqual(
            result.error.details["cause_code"],
            "HARNESS.MODEL.ACCEPTANCE_FAILURE",
        )
        self.assertEqual(model.calls, 1)
        self.assertEqual(tool.calls, 0)
        serialized = json.dumps(
            {
                "spans": [span.model_dump(mode="json") for span in tracer.spans()],
                "events": [event.model_dump(mode="json") for event in app.event_publisher.events()],
            },
            sort_keys=True,
        )
        self.assertNotIn(raw_secret, serialized)


if __name__ == "__main__":
    unittest.main()
