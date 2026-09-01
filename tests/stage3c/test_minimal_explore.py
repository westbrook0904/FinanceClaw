"""Agent Foundation F4b standalone Minimal Explore Loop 验收。"""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Callable, Sequence

from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityCompletionMode,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    Continuation,
    EgressType,
    ErrorCode,
    ExecutionMode,
    ExplorationBudget,
    ExplorationProfile,
    InvocationContext,
    ModelProviderFeatures,
    ModelUsage,
    PlanExecutionRecord,
    ProviderDescriptor,
    Request,
    RequestInput,
    RequestOptions,
    ResultEnvelope,
    ResultOutput,
    ResultStatus,
    SideEffectType,
    StructuredOutputSpec,
)
from harness_model import (
    GenerateRequest,
    GenerateResult,
    ModelFinishReason,
    ModelOutput,
    ModelProvider,
    ModelResponseFormat,
    PreparedStructuredOutput,
)
from harness_model.schema import structured_schema_hash
from harness_policy import Policy, PolicyContext, PolicyDecision, PolicyPhase
from harness_registry import InMemoryCapabilityRegistry
from harness_spi import Capability, PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_state import InMemoryStateStore, StateStore
from harness_trace import InMemoryTracer, SpanType

MODEL_ID = "model.explore/v1"
TOOL_ID = "explore.lookup/v1"
MODEL_PROVIDER_ID = "explore-model-provider"


class QueuedStrictModel(ModelProvider):
    def __init__(self, behavior: str = "success") -> None:
        self.behavior = behavior
        self.calls = 0
        self.requests: list[GenerateRequest] = []

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=MODEL_ID,
            name="Explore model",
            type=CapabilityType.MODEL,
            version="1.0.0",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )

    @property
    def features(self) -> ModelProviderFeatures:
        return ModelProviderFeatures(
            json_object=True,
            json_schema=True,
            json_schema_strict=True,
            usage_tokens=True,
        )

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput:
        return PreparedStructuredOutput(
            provider_id=MODEL_PROVIDER_ID,
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
        )

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        raise AssertionError("required structured generation must use generate_prepared")

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        del prepared, context
        self.calls += 1
        self.requests.append(request)
        data = self._turn(request)
        return GenerateResult.success(
            ModelOutput(type=ModelResponseFormat.JSON, data=data),
            ModelUsage(input_tokens=5, output_tokens=5, total_tokens=10),
            finish_reason=ModelFinishReason.STOP,
            provider_id=MODEL_PROVIDER_ID,
        )

    def _turn(self, request: GenerateRequest) -> dict[str, object]:
        if self.behavior == "invalid_input":
            return {
                "kind": "call_capability",
                "capability_id": TOOL_ID,
                "input": {"type": "json", "content": {"wrong": True}},
                "reason_code": "LOOKUP_REQUIRED",
            }
        if self.calls == 1:
            return {
                "kind": "call_capability",
                "capability_id": TOOL_ID,
                "input": {"type": "json", "content": {"query": "safe query"}},
                "reason_code": "LOOKUP_REQUIRED",
            }
        payload = json.loads(request.messages[-1].content)
        observations = [
            item["content"]
            for item in payload["context"]["items"]
            if item["source_kind"] == "observation"
        ]
        evidence_ref = observations[-1]["observation_id"]
        return {
            "kind": "finish",
            "output": {"type": "answer", "data": {"value": 7}},
            "evidence_refs": [evidence_ref],
            "reason_code": "EVIDENCE_SUFFICIENT",
        }


class ExploreTool(ToolSPI):
    def __init__(self, *, accepted: bool = False, blocking: bool = False) -> None:
        self.accepted = accepted
        self.blocking = blocking
        self.calls = 0
        self.started = asyncio.Event()

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=TOOL_ID,
            name="Explore lookup",
            type=CapabilityType.TOOL,
            version="1.0.0",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            execution_profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.READ,
                egress=EgressType.INTERNAL,
                completion_mode=CapabilityCompletionMode.SYNC,
            ),
        )

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        del context
        self.calls += 1
        self.started.set()
        if self.blocking:
            await asyncio.Event().wait()
        if self.accepted:
            return ResultEnvelope.accepted(
                Continuation(
                    job_ref="unexpected-job",
                    waiting_reason="provider_returned_async",
                )
            )
        return ResultEnvelope.success(
            ResultOutput(
                type="lookup",
                data={"query": request.arguments["query"], "value": 7},
            )
        )


class ExplorePlugin(PluginSPI):
    def __init__(self, tool: ExploreTool) -> None:
        self.tool = tool

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="explore-tools",
            name="Explore tools",
            version="1.0.0",
            sdk_version="1",
            capabilities=(TOOL_ID,),
        )

    def capabilities(self) -> Sequence[Capability]:
        return (self.tool,)

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class DenyExploreAction(Policy):
    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_EXECUTE})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        assert context.capability is not None
        if context.capability.id == TOOL_ID:
            return PolicyDecision.deny(self.name, reason="test action deny")
        return PolicyDecision.allow(self.name, reason="test allow")


class FaultInjectingStore(StateStore):
    def __init__(self, predicate: Callable[[PlanExecutionRecord], bool]) -> None:
        self.inner = InMemoryStateStore()
        self.predicate = predicate
        self.failed = False
        self.plan_id: str | None = None

    async def create(self, record: PlanExecutionRecord) -> None:
        self.plan_id = record.plan_id
        await self.inner.create(record)

    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        return await self.inner.load(plan_id)

    async def save(self, record: PlanExecutionRecord) -> None:
        if not self.failed and self.predicate(record):
            self.failed = True
            raise RuntimeError("injected checkpoint failure")
        await self.inner.save(record)

    async def delete(self, plan_id: str) -> None:
        await self.inner.delete(plan_id)


def profile(*, memory_required: bool = False) -> ExplorationProfile:
    return ExplorationProfile(
        profile_id="bounded-explorer",
        model_capability_id=MODEL_ID,
        allowed_capability_ids=frozenset({TOOL_ID}),
        default_budget=ExplorationBudget(
            max_steps=3,
            max_model_calls=3,
            max_action_calls=2,
            max_repeated_actions=0,
            max_observations=2,
        ),
        prompt_version="explore-v1",
        memory_required=memory_required,
    )


def request() -> Request:
    return Request(
        request_id="explore-request",
        input=RequestInput(type="goal", content={"secret": "top-secret-request"}),
        options=RequestOptions(execution_mode=ExecutionMode.EXPLORE),
    )


def build(
    model: QueuedStrictModel,
    tool: ExploreTool,
    *,
    configured_profile: ExplorationProfile | None = None,
    policies: tuple[Policy, ...] | None = None,
    state_store: StateStore | None = None,
    tracer: InMemoryTracer | None = None,
):
    registry = InMemoryCapabilityRegistry()
    registry.register_provider(
        model,
        descriptor=ProviderDescriptor(
            provider_id=MODEL_PROVIDER_ID,
            capability_id=MODEL_ID,
            plugin_id="explore-models",
            implementation_version="1.0.0",
        ),
    )
    profiles = (configured_profile or profile(),)
    return build_harness(
        registry=registry,
        plugins=(ExplorePlugin(tool),),
        policies=policies,
        exploration_profiles=profiles,
        single_writer_guaranteed=True,
        state_store=state_store,
        tracer=tracer,
        entry_point_group=None,
    )


def child(record: PlanExecutionRecord):
    return next(iter(record.state.explorations.values()))


class MinimalExploreTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_persists_observations_and_safe_trace(self) -> None:
        model = QueuedStrictModel()
        tool = ExploreTool()
        tracer = InMemoryTracer()
        app = build(model, tool, tracer=tracer)
        await app.start()

        result = await app.handle(request())

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.data, {"value": 7})
        self.assertEqual(result.metadata["execution_mode"], "explore")
        self.assertEqual(model.calls, 2)
        self.assertEqual(tool.calls, 1)
        record = await app.state_store.load(result.metadata["plan_id"])
        assert record is not None
        exploration = child(record)
        self.assertEqual(exploration.status.value, "succeeded")
        self.assertEqual(exploration.usage.model_calls, 2)
        self.assertEqual(exploration.usage.action_calls, 1)
        self.assertEqual(len(exploration.context_uses), 2)
        self.assertEqual(len(exploration.observations), 1)
        self.assertIsNone(exploration.pending_action_id)
        span_types = {span.type for span in tracer.spans(trace_id=result.trace_id)}
        self.assertIn(SpanType.EXPLORATION, span_types)
        self.assertIn(SpanType.ACTION, span_types)
        trace_json = json.dumps(
            [span.model_dump(mode="json") for span in tracer.spans()],
            ensure_ascii=False,
        )
        self.assertNotIn("top-secret-request", trace_json)
        await app.shutdown()

    async def test_unconfigured_and_single_writer_guards_fail_closed(self) -> None:
        app = build_harness(entry_point_group=None)
        await app.start()
        result = await app.handle(request())
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, ErrorCode.ROUTE_MODE_NOT_AVAILABLE)
        await app.shutdown()

        with self.assertRaisesRegex(ValueError, "single_writer_guaranteed"):
            build_harness(
                exploration_profiles=(profile(),),
                entry_point_group=None,
            )

        model = QueuedStrictModel()
        enabled = build(model, ExploreTool())
        await enabled.start()
        hybrid_request = request().model_copy(
            update={"options": RequestOptions(execution_mode=ExecutionMode.HYBRID)}
        )
        hybrid = await enabled.handle(hybrid_request)
        self.assertEqual(hybrid.status, ResultStatus.FAILED)
        self.assertEqual(hybrid.error.code, ErrorCode.ROUTE_MODE_NOT_AVAILABLE)
        self.assertEqual(model.calls, 0)
        await enabled.shutdown()

    async def test_policy_denial_has_zero_tool_outbound_and_terminal_checkpoint(self) -> None:
        model = QueuedStrictModel()
        tool = ExploreTool()
        app = build(model, tool, policies=(DenyExploreAction(),))
        await app.start()

        result = await app.handle(request())

        self.assertEqual(result.status, ResultStatus.DENIED)
        self.assertEqual(tool.calls, 0)
        record = await app.state_store.load(result.metadata["plan_id"])
        assert record is not None
        exploration = child(record)
        self.assertEqual(exploration.status.value, "denied")
        self.assertEqual(exploration.actions[0].status, "denied")
        self.assertIsNone(exploration.pending_action_id)
        self.assertFalse(exploration.observations)
        await app.shutdown()

    async def test_sync_capability_accepted_is_orphaned_and_never_waits(self) -> None:
        model = QueuedStrictModel()
        tool = ExploreTool(accepted=True)
        app = build(model, tool)
        await app.start()

        result = await app.handle(request())

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(
            result.error.code,
            ErrorCode.EXPLORATION_ASYNC_CONTRACT_VIOLATION,
        )
        self.assertIsNone(result.continuation)
        record = await app.state_store.load(result.metadata["plan_id"])
        assert record is not None
        action = child(record).actions[0]
        self.assertEqual(action.status, "orphaned")
        self.assertEqual(action.result.status, ResultStatus.ACCEPTED)
        self.assertEqual(action.result.continuation.job_ref, "unexpected-job")
        self.assertFalse(record.state.pending_jobs)
        await app.shutdown()

    async def test_resume_continues_only_from_completed_observation_boundary(self) -> None:
        model = QueuedStrictModel()
        tool = ExploreTool()
        store = FaultInjectingStore(
            lambda record: bool(
                record.state.explorations
                and len(child(record).observations) == 1
                and len(child(record).context_uses) == 2
            )
        )
        app = build(model, tool, state_store=store)
        await app.start()

        interrupted = await app.handle(request())
        self.assertEqual(interrupted.status, ResultStatus.FAILED)
        self.assertEqual(model.calls, 1)
        assert store.plan_id is not None

        resumed = await app.resume_plan(store.plan_id)

        self.assertEqual(resumed.status, ResultStatus.SUCCESS)
        self.assertEqual(model.calls, 2)
        self.assertEqual(tool.calls, 1)
        record = await store.load(store.plan_id)
        assert record is not None
        self.assertEqual(child(record).status.value, "succeeded")
        await app.shutdown()

    async def test_resume_rejects_proposed_action_without_reinvoking_tool(self) -> None:
        model = QueuedStrictModel()
        tool = ExploreTool()
        store = FaultInjectingStore(
            lambda record: bool(
                record.state.explorations
                and child(record).actions
                and child(record).actions[-1].status == "running"
            )
        )
        app = build(model, tool, state_store=store)
        await app.start()

        interrupted = await app.handle(request())
        self.assertEqual(interrupted.status, ResultStatus.FAILED)
        self.assertEqual(tool.calls, 0)
        assert store.plan_id is not None

        resumed = await app.resume_plan(store.plan_id)

        self.assertEqual(resumed.status, ResultStatus.FAILED)
        self.assertEqual(resumed.error.code, ErrorCode.EXPLORATION_RESUME_UNSAFE)
        self.assertEqual(tool.calls, 0)
        await app.shutdown()

    async def test_memory_required_and_invalid_action_fail_before_tool_outbound(self) -> None:
        memory_model = QueuedStrictModel()
        memory_tool = ExploreTool()
        memory_app = build(
            memory_model,
            memory_tool,
            configured_profile=profile(memory_required=True),
        )
        await memory_app.start()
        missing_memory = await memory_app.handle(request())
        self.assertEqual(missing_memory.status, ResultStatus.FAILED)
        self.assertEqual(missing_memory.error.code, ErrorCode.EXPLORATION_MEMORY_REQUIRED)
        self.assertEqual(memory_model.calls, 0)
        self.assertEqual(memory_tool.calls, 0)
        await memory_app.shutdown()

        invalid_model = QueuedStrictModel("invalid_input")
        invalid_tool = ExploreTool()
        invalid_app = build(invalid_model, invalid_tool)
        await invalid_app.start()
        invalid = await invalid_app.handle(request())
        self.assertEqual(invalid.status, ResultStatus.FAILED)
        self.assertEqual(invalid.error.code, ErrorCode.EXPLORATION_INVALID_TURN)
        self.assertEqual(invalid_model.calls, 2)
        self.assertEqual(invalid_tool.calls, 0)
        await invalid_app.shutdown()

    async def test_cancel_plan_cancels_active_action_and_persists_terminal_state(self) -> None:
        model = QueuedStrictModel()
        tool = ExploreTool(blocking=True)
        store = FaultInjectingStore(lambda record: False)
        app = build(model, tool, state_store=store)
        await app.start()
        handling = asyncio.create_task(app.handle(request()))
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        assert store.plan_id is not None

        cancelled = await app.cancel_plan(store.plan_id, "test cancellation")
        result = await asyncio.wait_for(handling, timeout=1)

        self.assertTrue(cancelled)
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        record = await store.load(store.plan_id)
        assert record is not None
        exploration = child(record)
        self.assertEqual(exploration.status.value, "cancelled")
        self.assertEqual(exploration.actions[0].status, "cancelled")
        self.assertIsNone(exploration.pending_action_id)
        await app.shutdown()
