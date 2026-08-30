"""Stage 3C Step 1: Plan identity/materialization acceptance gate."""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ExecutionMode,
    ExecutionPlan,
    InvocationContext,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestInput,
    RequestOptions,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
)
from harness_planning import (
    PlanIdentityFactory,
    Planner,
    PlanningContext,
    PlanTemplate,
    StaticPlanner,
)
from harness_spi import PluginManifest, PluginSPI, ToolRequest, ToolSPI
from pydantic import ValidationError

TOOL_ID = "stage3c.identity.echo/v1"


class EchoTool(ToolSPI):
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=TOOL_ID,
            name="Stage 3C identity echo",
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        return ResultEnvelope.success(ResultOutput(type="json", data={"ok": True}))


class ToolPlugin(PluginSPI):
    def __init__(self) -> None:
        self._tool = EchoTool()

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="stage3c-identity-tools",
            name="Stage 3C identity tools",
            version="1.0.0",
            sdk_version="1",
            capabilities=(TOOL_ID,),
        )

    def capabilities(self) -> tuple[ToolSPI, ...]:
        return (self._tool,)

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class SequenceIdentity:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"{self.prefix}-{self.calls}"


class RepeatedCandidatePlanner(Planner):
    def __init__(self, candidate: ExecutionPlan) -> None:
        self._candidate = candidate
        self.calls = 0

    @property
    def planner_id(self) -> str:
        return "repeated-candidate"

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        self.calls += 1
        return self._candidate


class NativeTemplatePlanner(Planner):
    def __init__(self) -> None:
        self.artifact_calls = 0

    @property
    def planner_id(self) -> str:
        return "native-template"

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        raise AssertionError("handle must prefer the overridden plan_artifact method")

    async def plan_artifact(self, context: PlanningContext) -> PlanTemplate:
        self.artifact_calls += 1
        return capability_template()


def plan_request(request_id: str) -> Request:
    return Request(
        request_id=request_id,
        input=RequestInput(type="identity-goal", content={"goal": "echo"}),
        options=RequestOptions(execution_mode=ExecutionMode.PLAN, trace=False),
    )


def capability_template() -> PlanTemplate:
    return PlanTemplate(nodes=(PlanNode(node_id="echo", capability=TOOL_ID),))


class PlanIdentityMaterializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_identity_factory_fails_before_execution(self) -> None:
        planner = StaticPlanner(
            "invalid-identity-template",
            {"identity-goal": capability_template()},
        )
        app = build_harness(
            plugins=(ToolPlugin(),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            plan_identity_factory=PlanIdentityFactory(lambda: ""),
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(plan_request("invalid-identity-request"))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.PLAN.IDENTITY_GENERATION_FAILED")

    async def test_native_plan_artifact_override_is_the_coordinator_input(self) -> None:
        sequence = SequenceIdentity("native-fresh")
        planner = NativeTemplatePlanner()
        app = build_harness(
            plugins=(ToolPlugin(),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            plan_identity_factory=PlanIdentityFactory(sequence),
            entry_point_group=None,
        )
        await app.start()
        try:
            result = await app.handle(plan_request("native-request"))
        finally:
            await app.shutdown()

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.metadata["plan_id"], "native-fresh-1")
        self.assertEqual(planner.artifact_calls, 1)
        self.assertEqual(sequence.calls, 1)

    async def test_static_template_gets_one_fresh_identity_per_handle(self) -> None:
        sequence = SequenceIdentity("static-fresh")
        planner = StaticPlanner(
            "static-template",
            {"identity-goal": capability_template()},
        )
        app = build_harness(
            plugins=(ToolPlugin(),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            plan_identity_factory=PlanIdentityFactory(sequence),
            entry_point_group=None,
        )
        await app.start()
        try:
            first = await app.handle(plan_request("static-request-1"))
            second = await app.handle(plan_request("static-request-2"))
        finally:
            await app.shutdown()

        self.assertEqual(first.status, ResultStatus.SUCCESS)
        self.assertEqual(second.status, ResultStatus.SUCCESS)
        self.assertEqual(first.metadata["plan_id"], "static-fresh-1")
        self.assertEqual(second.metadata["plan_id"], "static-fresh-2")
        self.assertNotEqual(first.metadata["plan_id"], second.metadata["plan_id"])
        self.assertEqual(first.metadata["plan_revision"], 1)
        self.assertEqual(second.metadata["plan_revision"], 1)
        self.assertEqual(sequence.calls, 2)
        self.assertIsNotNone(await app.state_store.load("static-fresh-1"))
        self.assertIsNotNone(await app.state_store.load("static-fresh-2"))

    async def test_legacy_planner_candidate_identity_is_never_trusted(self) -> None:
        sequence = SequenceIdentity("custom-fresh")
        planner = RepeatedCandidatePlanner(
            ExecutionPlan(
                plan_id="malicious-reused-id",
                revision=77,
                nodes=(PlanNode(node_id="echo", capability=TOOL_ID),),
                metadata={
                    "request_id": "forged-request",
                    "provider_id": "forged-provider",
                    "safe_label": "preserved",
                },
            )
        )
        app = build_harness(
            plugins=(ToolPlugin(),),
            planners=(planner,),
            default_planner_id=planner.planner_id,
            plan_identity_factory=PlanIdentityFactory(sequence),
            entry_point_group=None,
        )
        await app.start()
        try:
            first = await app.handle(plan_request("custom-request-1"))
            second = await app.handle(plan_request("custom-request-2"))
            first_record = await app.state_store.load("custom-fresh-1")
            second_record = await app.state_store.load("custom-fresh-2")
        finally:
            await app.shutdown()

        self.assertEqual(first.status, ResultStatus.SUCCESS)
        self.assertEqual(second.status, ResultStatus.SUCCESS)
        self.assertEqual(planner.calls, 2)
        self.assertEqual(sequence.calls, 2)
        self.assertEqual(first_record.plan.plan_id, "custom-fresh-1")
        self.assertEqual(second_record.plan.plan_id, "custom-fresh-2")
        self.assertEqual(first_record.plan.revision, 1)
        self.assertEqual(second_record.plan.revision, 1)
        self.assertEqual(first_record.plan.metadata["request_id"], "custom-request-1")
        self.assertNotIn("provider_id", first_record.plan.metadata)
        self.assertEqual(first_record.plan.metadata["safe_label"], "preserved")

    async def test_resume_keeps_materialized_identity(self) -> None:
        sequence = SequenceIdentity("waiting-fresh")
        planner = StaticPlanner(
            "waiting-template",
            {
                "identity-goal": PlanTemplate(
                    nodes=(
                        PlanNode(node_id="approve", kind=PlanNodeKind.APPROVAL),
                    )
                )
            },
        )
        app = build_harness(
            planners=(planner,),
            default_planner_id=planner.planner_id,
            plan_identity_factory=PlanIdentityFactory(sequence),
            entry_point_group=None,
        )
        await app.start()
        try:
            waiting = await app.handle(plan_request("waiting-request"))
            resumed = await app.resume_plan(waiting.continuation.plan_id)
        finally:
            await app.shutdown()

        self.assertEqual(waiting.status, ResultStatus.ACCEPTED)
        self.assertEqual(resumed.status, ResultStatus.ACCEPTED)
        self.assertEqual(waiting.continuation.plan_id, "waiting-fresh-1")
        self.assertEqual(resumed.continuation.plan_id, "waiting-fresh-1")
        self.assertEqual(sequence.calls, 1)

    async def test_execute_plan_bypasses_materializer_and_conflicts_on_repeat(self) -> None:
        sequence = SequenceIdentity("must-not-run")
        app = build_harness(
            plugins=(ToolPlugin(),),
            plan_identity_factory=PlanIdentityFactory(sequence),
            entry_point_group=None,
        )
        concrete = ExecutionPlan(
            plan_id="caller-owned-execution",
            revision=9,
            nodes=(PlanNode(node_id="echo", capability=TOOL_ID),),
        )
        request = Request(input=RequestInput(type="json", content={}))
        await app.start()
        try:
            first = await app.execute_plan(request, concrete)
            duplicate = await app.execute_plan(request, concrete)
        finally:
            await app.shutdown()

        self.assertEqual(first.status, ResultStatus.SUCCESS)
        self.assertEqual(first.metadata["plan_id"], "caller-owned-execution")
        self.assertEqual(first.metadata["plan_revision"], 9)
        self.assertEqual(duplicate.status, ResultStatus.FAILED)
        self.assertEqual(duplicate.error.code, "HARNESS.PLAN.EXECUTION_ID_CONFLICT")
        self.assertEqual(sequence.calls, 0)

    def test_plan_template_rejects_execution_identity_and_runtime_metadata(self) -> None:
        with self.assertRaises(ValidationError):
            PlanTemplate(
                plan_id="model-owned",  # type: ignore[call-arg]
                revision=9,  # type: ignore[call-arg]
                nodes=(PlanNode(node_id="echo", capability=TOOL_ID),),
            )
        with self.assertRaises(ValidationError):
            PlanTemplate(
                nodes=(PlanNode(node_id="echo", capability=TOOL_ID),),
                metadata={"request_id": "model-owned"},
            )


if __name__ == "__main__":
    unittest.main()
