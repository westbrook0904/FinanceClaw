"""Stage 3B Step 7 LLMPlanner 的自主规划与安全边界测试。"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    IdentityContext,
    InvocationContext,
    PlanningError,
    ProviderError,
    Request,
    RequestInput,
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
from harness_planning import (
    LLMPlanner,
    PlanDraft,
    PlanningConstraints,
    PlanningContext,
    PlanValidator,
)
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_routing import SafeRequestProjector
from harness_spi import ToolRequest, ToolSPI
from harness_trace import InMemoryTracer
from pydantic import ValidationError

MODEL_ID = "model.plan/v1"
FETCH_ID = "data.fetch/v1"
RANK_ID = "data.rank/v1"


def valid_draft(**extra: object) -> dict[str, object]:
    return {
        "budget": {"max_concurrency": 2},
        "failure_policy": "fail_fast",
        "nodes": [
            {
                "node_id": "fetch",
                "capability": FETCH_ID,
                "input_mapping": {"query": {"kind": "request", "pointer": "/input/content"}},
            },
            {"node_id": "rank", "capability": RANK_ID},
        ],
        "edges": [{"from_node": "fetch", "to_node": "rank"}],
        "outputs": {
            "ranking": {
                "kind": "node_output",
                "node_id": "rank",
                "pointer": "/output/data",
            }
        },
        **extra,
    }


class ScriptedPlanningModel(ModelProvider):
    def __init__(self, outcome: dict[str, object] | GenerateResult) -> None:
        self.outcome = outcome
        self.calls = 0
        self.requests: list[GenerateRequest] = []

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=MODEL_ID,
            name="Planning model",
            type=CapabilityType.MODEL,
            version="1.0.0",
            metadata={"provider_id": "must-not-leak"},
        )

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        self.calls += 1
        self.requests.append(request)
        if isinstance(self.outcome, GenerateResult):
            return self.outcome
        return GenerateResult.success(
            ModelOutput(type=ModelResponseFormat.JSON, data=self.outcome),
            ModelUsage(input_tokens=20, output_tokens=30, total_tokens=50),
            finish_reason=ModelFinishReason.STOP,
            provider_id="private-provider",
        )


class CountingTool(ToolSPI):
    def __init__(self, capability_id: str) -> None:
        self._capability_id = capability_id
        self.calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=self._capability_id,
            name=self._capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            metadata={
                "provider_id": "secret-provider",
                "plugin_id": "secret-plugin",
            },
        )

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ):
        self.calls += 1
        raise AssertionError("LLMPlanner must never execute a capability")


def make_planner(
    outcome: dict[str, object] | GenerateResult,
    *,
    plan_id: str = "plan-harness-owned",
    configured_scope: tuple[str, ...] | None = (FETCH_ID, RANK_ID),
) -> tuple[
    LLMPlanner,
    ScriptedPlanningModel,
    tuple[CountingTool, CountingTool],
    RegistryCapabilityCatalog,
]:
    registry = InMemoryCapabilityRegistry()
    model = ScriptedPlanningModel(outcome)
    tools = (CountingTool(FETCH_ID), CountingTool(RANK_ID))
    registry.register(model, plugin_id="planning-models")
    for tool in tools:
        registry.register(tool, plugin_id="business-tools")
    catalog = RegistryCapabilityCatalog(registry)
    planner = LLMPlanner(
        ModelGateway(registry, InMemoryTracer()),
        planner_model_capability_id=MODEL_ID,
        validator=PlanValidator(catalog),
        planner_id="llm-default",
        plan_id_factory=lambda: plan_id,
        allowed_capability_ids=configured_scope,
    )
    return planner, model, tools, catalog


def make_context(
    catalog: RegistryCapabilityCatalog,
    *,
    max_plan_nodes: int = 4,
    allowed_capability_ids: frozenset[str] | None = None,
    deadline_at: datetime | None = None,
) -> PlanningContext:
    effective_deadline = deadline_at or datetime.now(UTC) + timedelta(hours=1)
    request = Request(
        request_id="req-plan-goal",
        tenant_id="untrusted-tenant",
        user_id="untrusted-user",
        input=RequestInput(type="analysis", content={"goal": "fetch and rank"}),
        metadata={"locale": "zh-CN", "secret": "request-secret"},
    )
    invocation = InvocationContext(
        request=request,
        identity=IdentityContext(
            subject="secret-subject",
            attributes={"credential": "identity-secret"},
        ),
        deadline_at=effective_deadline,
        attributes={"runtime_secret": "context-secret"},
        trace_context=TraceContext(
            trace_id="trace-plan",
            span_id="planner-parent",
            baggage={"secret": "trace-secret"},
        ),
    )
    return PlanningContext(
        invocation=invocation,
        goal=SafeRequestProjector(metadata_allowlist=("locale",)).project(request),
        catalog_snapshot=catalog.list(),
        constraints=PlanningConstraints(
            max_plan_nodes=max_plan_nodes,
            allowed_capability_ids=(
                allowed_capability_ids
                if allowed_capability_ids is not None
                else frozenset({FETCH_ID, RANK_ID})
            ),
            deadline_at=effective_deadline,
        ),
    )


class PlanDraftTests(unittest.TestCase):
    def test_draft_round_trips_without_harness_owned_fields(self) -> None:
        draft = PlanDraft.model_validate(valid_draft())
        restored = PlanDraft.model_validate(draft.model_dump(mode="json"))

        self.assertEqual(restored, draft)
        for field_name in ("plan_id", "revision", "metadata"):
            self.assertNotIn(field_name, PlanDraft.model_fields)
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    PlanDraft.model_validate({**valid_draft(), field_name: "injected"})


class LLMPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_first_response_builds_a_harness_owned_execution_plan(self) -> None:
        planner, model, tools, catalog = make_planner(valid_draft())
        context = make_context(catalog)

        plan = await planner.plan(context)

        self.assertEqual(plan.plan_id, "plan-harness-owned")
        self.assertEqual(plan.revision, 1)
        self.assertEqual(
            dict(plan.metadata),
            {
                "planner_id": "llm-default",
                "prompt_version": "planner-v1",
                "request_id": "req-plan-goal",
            },
        )
        self.assertEqual([node.node_id for node in plan.nodes], ["fetch", "rank"])
        self.assertEqual(model.calls, 1)
        self.assertEqual([tool.calls for tool in tools], [0, 0])

        request = model.requests[0]
        self.assertEqual(request.model, MODEL_ID)
        self.assertEqual(request.response_format, ModelResponseFormat.JSON)
        self.assertEqual(request.temperature, 0.0)
        self.assertEqual(
            dict(request.metadata),
            {"purpose": "plan", "prompt_version": "planner-v1"},
        )
        self.assertEqual(
            request.model_dump(mode="json")["response_schema"],
            planner._response_schema,  # noqa: SLF001
        )
        prompt = json.loads(request.messages[-1].content)
        self.assertEqual(prompt["allowed_capability_ids"], [FETCH_ID, RANK_ID])
        self.assertEqual(
            [item["id"] for item in prompt["capability_catalog"]],
            [FETCH_ID, RANK_ID],
        )
        serialized = request.messages[-1].content
        for secret in (
            "secret-provider",
            "secret-plugin",
            "request-secret",
            "secret-subject",
            "identity-secret",
            "context-secret",
            "trace-secret",
            "private-provider",
        ):
            self.assertNotIn(secret, serialized)

    async def test_model_cannot_inject_plan_identity_or_plan_metadata(self) -> None:
        for field_name, value in (
            ("plan_id", "model-plan"),
            ("revision", 99),
            ("metadata", {"planner_id": "model-choice"}),
        ):
            with self.subTest(field_name=field_name):
                planner, model, tools, catalog = make_planner(valid_draft(**{field_name: value}))
                with self.assertRaises(PlanningError) as raised:
                    await planner.plan(make_context(catalog))
                self.assertEqual(raised.exception.code, ErrorCode.PLANNER_INVALID_OUTPUT)
                self.assertEqual(model.calls, 1)
                self.assertEqual([tool.calls for tool in tools], [0, 0])
                self.assertNotIn("model-choice", str(raised.exception.details))

    async def test_planning_guards_enforce_size_scope_deadline_and_metadata(self) -> None:
        future = datetime.now(UTC) + timedelta(minutes=30)
        too_many = valid_draft(
            nodes=[
                {"node_id": "one", "capability": FETCH_ID},
                {"node_id": "two", "capability": RANK_ID},
            ],
            edges=[],
            outputs={},
        )
        cases = (
            (too_many, {"max_plan_nodes": 1}, ErrorCode.PLANNER_PLAN_TOO_LARGE, None),
            (
                valid_draft(
                    nodes=[{"node_id": "unknown", "capability": "secret.tool/v1"}],
                    edges=[],
                    outputs={},
                ),
                {},
                ErrorCode.PLANNER_INVALID_OUTPUT,
                "capability_not_allowed",
            ),
            (
                valid_draft(
                    budget={
                        "max_concurrency": 1,
                        "deadline_at": (future + timedelta(hours=1)).isoformat(),
                    }
                ),
                {"deadline_at": future},
                ErrorCode.PLANNER_INVALID_OUTPUT,
                "deadline_exceeds_request",
            ),
            (
                valid_draft(
                    nodes=[
                        {
                            "node_id": "fetch",
                            "capability": FETCH_ID,
                            "metadata": {"provider_id": "injected-provider"},
                        }
                    ],
                    edges=[],
                    outputs={},
                ),
                {},
                ErrorCode.PLANNER_INVALID_OUTPUT,
                "reserved_metadata",
            ),
        )

        for outcome, context_kwargs, expected_code, reason in cases:
            with self.subTest(expected_code=expected_code, reason=reason):
                planner, model, tools, catalog = make_planner(outcome)
                with self.assertRaises(PlanningError) as raised:
                    await planner.plan(make_context(catalog, **context_kwargs))
                self.assertEqual(raised.exception.code, expected_code)
                if reason is not None:
                    self.assertEqual(raised.exception.details["reason"], reason)
                self.assertEqual(model.calls, 1)
                self.assertEqual([tool.calls for tool in tools], [0, 0])

    async def test_plan_validator_rejects_an_invalid_generated_dag(self) -> None:
        outcome = valid_draft(
            outputs={
                "missing": {
                    "kind": "node_output",
                    "node_id": "does-not-exist",
                    "pointer": "/output/data",
                }
            }
        )
        planner, model, tools, catalog = make_planner(outcome)

        with self.assertRaises(PlanningError) as raised:
            await planner.plan(make_context(catalog))

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_INVALID_OUTPUT)
        self.assertIn(
            "PLAN.OUTPUT_REFERENCE_NOT_FOUND",
            raised.exception.details["validation_codes"],
        )
        self.assertEqual(model.calls, 1)
        self.assertEqual([tool.calls for tool in tools], [0, 0])

    async def test_model_failure_maps_only_safe_cause_code(self) -> None:
        failure = ProviderError(
            "secret provider failure",
            code="HARNESS.MODEL.SECRET_FAILURE",
            details={"provider_id": "secret-provider", "raw": "secret-output"},
        )
        planner, model, tools, catalog = make_planner(GenerateResult.failure(failure.to_detail()))

        with self.assertRaises(PlanningError) as raised:
            await planner.plan(make_context(catalog))

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_MODEL_FAILED)
        self.assertEqual(
            raised.exception.details["cause_code"],
            "HARNESS.MODEL.SECRET_FAILURE",
        )
        self.assertNotIn("secret-provider", str(raised.exception.details))
        self.assertNotIn("secret-output", str(raised.exception.details))
        self.assertEqual(model.calls, 1)
        self.assertEqual([tool.calls for tool in tools], [0, 0])

    async def test_expired_deadline_fails_before_calling_the_model(self) -> None:
        planner, model, tools, catalog = make_planner(valid_draft())

        with self.assertRaises(PlanningError) as raised:
            await planner.plan(
                make_context(catalog, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
            )

        self.assertEqual(raised.exception.code, ErrorCode.PLANNER_DEADLINE_EXCEEDED)
        self.assertEqual(model.calls, 0)
        self.assertEqual([tool.calls for tool in tools], [0, 0])

    def test_llm_planner_source_has_no_execution_dependency(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "harness_planning" / "llm.py").read_text(
            encoding="utf-8"
        )

        for forbidden in ("CapabilityInvoker", "ExecutionEngine", "harness_execution"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
