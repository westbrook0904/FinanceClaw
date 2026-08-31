"""Stage 3B Acceptance 的确定性 Router、Planner、Model 与 Capability 夹具。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ExecutionMode,
    ExecutionPlan,
    InvocationContext,
    ModelProviderFeatures,
    NodeOutputBinding,
    PlanEdge,
    PlanExecutionRecord,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestBinding,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
    RouteDecision,
    RouteSource,
    RouteType,
    StructuredOutputSpec,
)
from harness_model import (
    GenerateRequest,
    GenerateResult,
    ModelFinishReason,
    ModelOutput,
    ModelProvider,
    ModelResponseFormat,
    ModelUsage,
    PreparedStructuredOutput,
)
from harness_model.schema import structured_schema_hash
from harness_planning import Planner, PlanningContext
from harness_routing import Router, RoutingContext
from harness_runtime import InvocationContextFactory
from harness_spi import Capability, PluginManifest, PluginSPI, ToolRequest, ToolSPI
from harness_state import InMemoryStateStore, StateStore

ROUTE_MODEL_ID = "stage3b.route-model/v1"
PLAN_MODEL_ID = "stage3b.plan-model/v1"
ECHO_TOOL_ID = "stage3b.echo/v1"
SECOND_TOOL_ID = "stage3b.second/v1"

type ModelOutcome = dict[str, object] | GenerateResult


class RecordingTool(ToolSPI):
    """返回输入并记录共享 InvocationContext 的本地 Tool。"""

    def __init__(self, capability_id: str = ECHO_TOOL_ID) -> None:
        self._capability_id = capability_id
        self.contexts: list[InvocationContext] = []

    @property
    def calls(self) -> int:
        return len(self.contexts)

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=self._capability_id,
            name=self._capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.contexts.append(context)
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data=request.model_dump(mode="json")["arguments"],
            )
        )


class AcceptancePlugin(PluginSPI):
    """把测试 Capability 通过真实 LocalPluginLoader 注册。"""

    def __init__(
        self,
        capabilities: Sequence[Capability],
        *,
        plugin_id: str = "stage3b-acceptance",
    ) -> None:
        self._capabilities = tuple(capabilities)
        self._plugin_id = plugin_id

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id=self._plugin_id,
            name=self._plugin_id,
            version="1.0.0",
            sdk_version="1",
            capabilities=tuple(item.descriptor().id for item in self._capabilities),
        )

    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class ScriptedModel(ModelProvider):
    """按序返回结构化结果，并记录 ModelGateway 输入。"""

    def __init__(
        self,
        model_id: str,
        outcomes: ModelOutcome | Sequence[ModelOutcome],
    ) -> None:
        self._model_id = model_id
        self.outcomes = (
            tuple(outcomes)
            if isinstance(outcomes, Sequence) and not isinstance(outcomes, dict | str)
            else (outcomes,)
        )
        if not self.outcomes:
            raise ValueError("at least one model outcome is required")
        self.requests: list[GenerateRequest] = []
        self.contexts: list[InvocationContext] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=self._model_id,
            name=self._model_id,
            type=CapabilityType.MODEL,
            version="1.0.0",
        )

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        return await self._generate(request, context)

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
        plugin_id = (
            "stage3b-plan-model"
            if self._model_id == PLAN_MODEL_ID
            else "stage3b-route-model"
        )
        return PreparedStructuredOutput(
            provider_id=f"{plugin_id}:{self._model_id}",
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
        )

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        return await self._generate(request, context)

    async def _generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        self.requests.append(request)
        self.contexts.append(context)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, GenerateResult):
            return outcome
        return GenerateResult.success(
            ModelOutput(type=ModelResponseFormat.JSON, data=outcome),
            ModelUsage(input_tokens=12, output_tokens=8, total_tokens=20),
            finish_reason=ModelFinishReason.STOP,
            provider_id=f"{self._model_id}:provider",
        )


class ScriptedRouter(Router):
    """记录 RoutingContext，并返回固定决策或异常。"""

    def __init__(
        self,
        outcome: RouteDecision | BaseException,
        *,
        router_id: str = "stage3b-scripted-router",
    ) -> None:
        self.outcome = outcome
        self._router_id = router_id
        self.contexts: list[RoutingContext] = []

    @property
    def router_id(self) -> str:
        return self._router_id

    async def route(self, context: RoutingContext) -> RouteDecision:
        self.contexts.append(context)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class ScriptedPlanner(Planner):
    """记录 PlanningContext，并返回固定计划或异常。"""

    def __init__(
        self,
        outcome: ExecutionPlan | BaseException,
        *,
        planner_id: str = "stage3b-scripted-planner",
        fail_if_called: bool = False,
    ) -> None:
        self.outcome = outcome
        self._planner_id = planner_id
        self.fail_if_called = fail_if_called
        self.contexts: list[PlanningContext] = []

    @property
    def planner_id(self) -> str:
        return self._planner_id

    @property
    def calls(self) -> int:
        return len(self.contexts)

    async def plan(self, context: PlanningContext) -> ExecutionPlan:
        if self.fail_if_called:
            raise AssertionError("Planner must not be called while resuming persisted state")
        self.contexts.append(context)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class RecordingContextFactory(InvocationContextFactory):
    """固定 Deadline，并证明 handle() 只创建一个 Context。"""

    def __init__(self, deadline_at: datetime) -> None:
        self.deadline_at = deadline_at
        self.requests: list[Request] = []

    def create(self, request: Request) -> InvocationContext:
        self.requests.append(request)
        return InvocationContext(request=request, deadline_at=self.deadline_at)


class CountingStateStore(StateStore):
    """委托内存实现并统计持久化调用，验证非法 Plan 零 checkpoint。"""

    def __init__(self) -> None:
        self._delegate = InMemoryStateStore()
        self.create_calls = 0
        self.save_calls = 0

    async def create(self, record: PlanExecutionRecord) -> None:
        self.create_calls += 1
        await self._delegate.create(record)

    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        return await self._delegate.load(plan_id)

    async def save(self, record: PlanExecutionRecord) -> None:
        self.save_calls += 1
        await self._delegate.save(record)

    async def delete(self, plan_id: str) -> None:
        await self._delegate.delete(plan_id)


def make_request(
    *,
    mode: ExecutionMode = ExecutionMode.AUTO,
    target: bool = False,
    input_type: str = "stage3b-goal",
    request_id: str = "stage3b-request",
    trace: bool = True,
) -> Request:
    return Request(
        request_id=request_id,
        input=RequestInput(type=input_type, content={"message": "stage3b-secret-goal"}),
        target=RequestTarget(capability=ECHO_TOOL_ID) if target else None,
        options=RequestOptions(execution_mode=mode, trace=trace),
    )


def fast_decision(
    *,
    capability_id: str = ECHO_TOOL_ID,
    source: RouteSource = RouteSource.RULE,
) -> RouteDecision:
    return RouteDecision(
        mode=ExecutionMode.FAST,
        route_type=RouteType.DIRECT_CAPABILITY,
        source=source,
        capability_id=capability_id,
        confidence=1.0,
        reason_code="STAGE3B_FAST",
    )


def plan_decision(*, source: RouteSource = RouteSource.RULE) -> RouteDecision:
    return RouteDecision(
        mode=ExecutionMode.PLAN,
        route_type=RouteType.GENERATED_PLAN,
        source=source,
        confidence=1.0,
        reason_code="STAGE3B_PLAN",
    )


def echo_plan(plan_id: str = "stage3b-echo-plan") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        nodes=(
            PlanNode(
                node_id="echo",
                capability=ECHO_TOOL_ID,
                input_mapping={"message": RequestBinding(pointer="/input/content/message")},
            ),
        ),
        outputs={
            "message": NodeOutputBinding(
                node_id="echo",
                pointer="/output/data/message",
            )
        },
    )


def approval_plan(plan_id: str = "stage3b-waiting-plan") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        nodes=(
            PlanNode(node_id="approval", kind=PlanNodeKind.APPROVAL),
            PlanNode(
                node_id="echo",
                capability=ECHO_TOOL_ID,
                input_mapping={"message": RequestBinding(pointer="/input/content/message")},
            ),
        ),
        edges=(PlanEdge(from_node="approval", to_node="echo"),),
        outputs={
            "message": NodeOutputBinding(
                node_id="echo",
                pointer="/output/data/message",
            )
        },
    )


def valid_plan_draft() -> dict[str, object]:
    return {
        "nodes": [
            {
                "node_id": "echo",
                "capability_id": ECHO_TOOL_ID,
                "input_mapping": {
                    "message": {
                        "kind": "request",
                        "pointer": "/input/content/message",
                    }
                },
            }
        ],
        "edges": [],
        "outputs": {
            "message": {
                "kind": "node_output",
                "node_id": "echo",
                "pointer": "/output/data/message",
            }
        },
    }
