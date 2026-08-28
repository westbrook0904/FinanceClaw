"""Stage 3B Step 6 LLMRouter 的结构化输出与安全边界测试。"""

from __future__ import annotations

import json
import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    ExecutionMode,
    IdentityContext,
    InvocationContext,
    PlanningError,
    ProviderDescriptor,
    ProviderError,
    Request,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    RouteSource,
    RoutingError,
    TraceContext,
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
from harness_routing import (
    LLMRouter,
    RouteDecisionValidator,
    RoutePolicyConstraints,
    RoutingContext,
    RuleRouter,
    SafeRequestProjector,
)
from harness_spi import ToolRequest, ToolSPI
from harness_trace import InMemoryTracer

MODEL_ID = "model.route/v1"
TOOL_ID = "finance.query/v1"


def descriptor(
    capability_id: str,
    capability_type: CapabilityType = CapabilityType.TOOL,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=capability_id,
        name=capability_id,
        type=capability_type,
        version="1.0.0",
        metadata={"public": True},
    )


def fast_decision(
    capability_id: str = TOOL_ID,
    *,
    source: str = "model",
    **extra: object,
) -> dict[str, object]:
    return {
        "mode": "fast",
        "route_type": "direct_capability",
        "source": source,
        "capability_id": capability_id,
        "confidence": 0.91,
        "reason_code": "MODEL_FAST",
        **extra,
    }


def plan_decision(planner_id: str = "planner-a") -> dict[str, object]:
    return {
        "mode": "plan",
        "route_type": "generated_plan",
        "source": "model",
        "planner_id": planner_id,
        "confidence": 0.82,
        "reason_code": "MODEL_PLAN",
    }


class ScriptedRouteModel(ModelProvider):
    def __init__(
        self,
        outcome: dict[str, object] | GenerateResult,
        *,
        model_id: str = MODEL_ID,
    ) -> None:
        self._descriptor = descriptor(model_id, CapabilityType.MODEL)
        self.outcome = outcome
        self.calls = 0
        self.requests: list[GenerateRequest] = []
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        self.calls += 1
        self.requests.append(request)
        self.contexts.append(context)
        if isinstance(self.outcome, GenerateResult):
            return self.outcome
        return GenerateResult.success(
            ModelOutput(type=ModelResponseFormat.JSON, data=self.outcome),
            ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason=ModelFinishReason.STOP,
        )


class CountingTool(ToolSPI):
    def __init__(self) -> None:
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return descriptor(TOOL_ID)

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        raise AssertionError("LLMRouter must not execute business capabilities")


def register_model(
    registry: InMemoryCapabilityRegistry,
    model: ScriptedRouteModel,
    provider_id: str,
    *,
    priority: int = 100,
) -> None:
    registry.register_provider(
        model,
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=MODEL_ID,
            plugin_id="route-models",
            implementation_version="1.0.0",
            priority=priority,
        ),
    )


def make_context(
    *,
    mode: ExecutionMode = ExecutionMode.AUTO,
    target: bool = False,
    constraints: RoutePolicyConstraints | None = None,
    include_model_descriptor: bool = False,
) -> RoutingContext:
    request = Request(
        request_id="req-llm-route",
        tenant_id="untrusted-tenant",
        user_id="untrusted-user",
        input=RequestInput(type="finance-goal", content={"query": "compare"}),
        target=(RequestTarget(capability=TOOL_ID, plugin="secret-plugin-pin") if target else None),
        metadata={"locale": "zh-CN", "secret": "request-secret"},
        options=RequestOptions(execution_mode=mode),
    )
    invocation = InvocationContext(
        request=request,
        identity=IdentityContext(
            subject="trusted-secret-subject",
            attributes={"credential": "identity-secret"},
        ),
        attributes={"runtime_secret": "context-secret"},
        trace_context=TraceContext(
            trace_id="trace-route",
            span_id="runtime-span",
            baggage={"secret": "trace-secret"},
        ),
    )
    catalog = [descriptor(TOOL_ID)]
    if include_model_descriptor:
        catalog.append(descriptor(MODEL_ID, CapabilityType.MODEL))
    return RoutingContext(
        invocation=invocation,
        request_summary=SafeRequestProjector(metadata_allowlist=("locale",)).project(request),
        requested_mode=mode,
        catalog_snapshot=tuple(catalog),
        constraints=constraints or RoutePolicyConstraints(),
    )


def make_router(
    model: ScriptedRouteModel,
    *,
    planner_ids: tuple[str, ...] = ("planner-a", "planner-b"),
) -> tuple[LLMRouter, InMemoryCapabilityRegistry]:
    registry = InMemoryCapabilityRegistry()
    register_model(registry, model, "route-provider")
    validator = RouteDecisionValidator(planner_ids=planner_ids)
    return (
        LLMRouter(
            ModelGateway(registry, InMemoryTracer()),
            route_model_capability_id=MODEL_ID,
            decision_validator=validator,
        ),
        registry,
    )


class LLMRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_decision_uses_safe_structured_prompt(self) -> None:
        model = ScriptedRouteModel(fast_decision())
        router, _ = make_router(model)
        context = make_context(
            target=True,
            include_model_descriptor=True,
            constraints=RoutePolicyConstraints(
                allowed_modes=frozenset({ExecutionMode.FAST}),
                allowed_capability_ids=frozenset({TOOL_ID, "not-in-catalog/v1"}),
                allowed_planner_ids=frozenset({"planner-a", "not-configured"}),
            ),
        )

        decision = await router.route(context)

        self.assertEqual(decision.capability_id, TOOL_ID)
        self.assertEqual(decision.source, RouteSource.MODEL)
        self.assertEqual(model.calls, 1)
        request = model.requests[0]
        self.assertEqual(request.model, MODEL_ID)
        self.assertEqual(request.response_format, ModelResponseFormat.JSON)
        self.assertEqual(request.temperature, 0.0)
        self.assertEqual(
            dict(request.metadata),
            {"purpose": "route", "prompt_version": "route-v1"},
        )
        self.assertEqual(
            request.model_dump(mode="json")["response_schema"],
            router._response_schema,  # noqa: SLF001
        )

        prompt = json.loads(request.messages[-1].content)
        self.assertEqual(prompt["requested_mode"], "auto")
        self.assertEqual(prompt["allowed_modes"], ["fast"])
        self.assertEqual(prompt["allowed_capability_ids"], [TOOL_ID])
        self.assertEqual(prompt["available_planner_ids"], ["planner-a"])
        self.assertEqual(
            [item["id"] for item in prompt["capability_catalog"]],
            [TOOL_ID],
        )
        self.assertEqual(prompt["request_summary"]["metadata"], {"locale": "zh-CN"})
        serialized = request.messages[-1].content
        for secret in (
            "secret-plugin-pin",
            "request-secret",
            "trusted-secret-subject",
            "identity-secret",
            "context-secret",
            "trace-secret",
        ):
            self.assertNotIn(secret, serialized)

    async def test_rule_router_uses_llm_only_as_last_fallback(self) -> None:
        model = ScriptedRouteModel(fast_decision())
        llm_router, registry = make_router(model)
        tool = CountingTool()
        registry.register(tool, plugin_id="business-tools")
        router = RuleRouter(fallback=llm_router)

        decision = await router.route(make_context())

        self.assertEqual(decision.capability_id, TOOL_ID)
        self.assertEqual(model.calls, 1)
        self.assertEqual(tool.calls, 0)

    async def test_unknown_or_non_executable_capability_is_rejected(self) -> None:
        cases = (
            ("missing/v1", make_context()),
            (MODEL_ID, make_context(include_model_descriptor=True)),
        )
        for capability_id, context in cases:
            with self.subTest(capability_id=capability_id):
                model = ScriptedRouteModel(fast_decision(capability_id))
                router, _ = make_router(model)

                with self.assertRaises(RoutingError) as raised:
                    await router.route(context)

                self.assertEqual(raised.exception.code, ErrorCode.ROUTE_INVALID_DECISION)

    async def test_fixed_plan_cannot_be_changed_to_fast(self) -> None:
        model = ScriptedRouteModel(fast_decision())
        router, _ = make_router(model)

        with self.assertRaises(RoutingError) as raised:
            await router.route(make_context(mode=ExecutionMode.PLAN))

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_INVALID_DECISION)
        self.assertEqual(raised.exception.details["requested_mode"], "plan")

    async def test_unconfigured_planner_is_rejected(self) -> None:
        model = ScriptedRouteModel(plan_decision("missing-planner"))
        router, _ = make_router(model)

        with self.assertRaises(PlanningError) as raised:
            await router.route(make_context())

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_NOT_CONFIGURED)

    async def test_provider_and_plugin_fields_are_rejected_without_echoing_output(self) -> None:
        for field_name in ("provider_id", "plugin_id"):
            with self.subTest(field_name=field_name):
                model = ScriptedRouteModel(fast_decision(**{field_name: "secret-model-pin"}))
                router, _ = make_router(model)

                with self.assertRaises(RoutingError) as raised:
                    await router.route(make_context())

                self.assertEqual(raised.exception.code, ErrorCode.ROUTE_INVALID_DECISION)
                self.assertEqual(
                    raised.exception.details["reason"],
                    "invalid_model_output",
                )
                self.assertNotIn("secret-model-pin", str(raised.exception.details))

    async def test_non_model_source_is_rejected(self) -> None:
        model = ScriptedRouteModel(fast_decision(source="rule"))
        router, _ = make_router(model)

        with self.assertRaises(RoutingError) as raised:
            await router.route(make_context())

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_INVALID_DECISION)
        self.assertEqual(
            raised.exception.details["reason"],
            "invalid_decision_source",
        )

    async def test_gateway_failure_maps_only_safe_cause_code(self) -> None:
        failure = ProviderError(
            "secret provider failure message",
            code="HARNESS.MODEL.SECRET_FAILURE",
            details={
                "provider_id": "secret-provider",
                "raw_response": "secret-model-output",
            },
        )
        model = ScriptedRouteModel(GenerateResult.failure(failure.to_detail()))
        router, _ = make_router(model)

        with self.assertRaises(RoutingError) as raised:
            await router.route(make_context())

        self.assertEqual(raised.exception.code, ErrorCode.ROUTE_MODEL_FAILED)
        self.assertEqual(
            raised.exception.details["cause_code"],
            "HARNESS.MODEL.SECRET_FAILURE",
        )
        serialized = str(raised.exception.details)
        self.assertNotIn("secret-provider", serialized)
        self.assertNotIn("secret-model-output", serialized)
        self.assertNotIn("secret provider failure message", serialized)

    async def test_model_provider_fallback_remains_owned_by_gateway(self) -> None:
        transient = ProviderError(
            "primary unavailable",
            code="HARNESS.MODEL.PRIMARY_UNAVAILABLE",
            retryable=False,
            fallbackable=True,
        )
        primary = ScriptedRouteModel(GenerateResult.failure(transient.to_detail()))
        backup = ScriptedRouteModel(fast_decision())
        registry = InMemoryCapabilityRegistry()
        register_model(registry, primary, "primary", priority=100)
        register_model(registry, backup, "backup", priority=10)
        router = LLMRouter(
            ModelGateway(registry, InMemoryTracer()),
            route_model_capability_id=MODEL_ID,
            decision_validator=RouteDecisionValidator(),
        )

        decision = await router.route(make_context())

        self.assertEqual(decision.capability_id, TOOL_ID)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 1)


if __name__ == "__main__":
    unittest.main()
