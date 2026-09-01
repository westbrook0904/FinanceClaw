"""Plan 模板归一化与 fresh execution identity 物化边界。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

from harness_contracts import (
    ContractModel,
    ErrorCode,
    ExecutionPlan,
    FailurePolicy,
    FrozenOutputMapping,
    InvocationContext,
    PlanBudget,
    PlanEdge,
    PlanningError,
    PlanNode,
)
from harness_contracts.base import FrozenJsonMapping
from pydantic import Field, field_validator, model_validator

from .draft import PlanDraft

type PlanIdFactory = Callable[[], str]

_RUNTIME_METADATA_KEYS = frozenset(
    {
        "plan_id",
        "revision",
        "request_id",
        "planner_id",
        "creator_id",
        "prompt_version",
        "provider_id",
        "plugin_id",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "execution_state",
        "checkpoint_state",
    }
)


class PlanTemplate(ContractModel):
    """不含运行身份的不可变 Plan DAG 模板。

    模板可以复用；``plan_id``、``revision`` 与 request/runtime metadata 只能由
    :class:`PlanMaterializer` 在 fresh execution 边界写入。
    """

    budget: PlanBudget = Field(default_factory=PlanBudget)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    nodes: tuple[PlanNode, ...]
    edges: tuple[PlanEdge, ...] = ()
    outputs: FrozenOutputMapping = Field(default_factory=dict)
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def reject_runtime_metadata(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        reserved = sorted(set(value).intersection(_RUNTIME_METADATA_KEYS))
        if reserved:
            raise ValueError(
                "plan template metadata contains runtime-owned keys: " + ", ".join(reserved)
            )
        return value

    @model_validator(mode="after")
    def validate_unique_node_ids(self) -> PlanTemplate:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("plan node_id values must be unique")
        return self


type PlannerArtifact = PlanTemplate | ExecutionPlan


class PlanIdentityFactory:
    """为一次 fresh Plan execution 生成并校验稳定 identity。"""

    def __init__(self, factory: PlanIdFactory | None = None) -> None:
        if factory is not None and not callable(factory):
            raise TypeError("factory must be callable")
        self._factory = factory or (lambda: f"plan-{uuid4().hex}")

    def new_plan_id(self) -> str:
        value = self._factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("plan identity factory must return a non-empty string")
        if value != value.strip():
            raise ValueError("plan identity factory must not return surrounding whitespace")
        return value

    def __call__(self) -> str:
        return self.new_plan_id()


class PlannerOutputNormalizer:
    """把原生模板或 legacy ExecutionPlan candidate 归一化为 PlanTemplate。"""

    def normalize(
        self,
        artifact: object,
        *,
        planner_id: str,
    ) -> PlanTemplate:
        if not isinstance(planner_id, str) or not planner_id.strip():
            raise TypeError("planner_id must be a non-empty string")
        if isinstance(artifact, PlanTemplate):
            return artifact
        if not isinstance(artifact, ExecutionPlan):
            raise PlanningError(
                "planner returned an unsupported plan artifact",
                code=ErrorCode.PLANNER_INVALID_OUTPUT,
                details={
                    "planner_id": planner_id,
                    "output_type": type(artifact).__name__,
                },
            )

        metadata = {
            key: value
            for key, value in artifact.metadata.items()
            if key not in _RUNTIME_METADATA_KEYS
        }
        try:
            return PlanTemplate(
                budget=artifact.budget,
                failure_policy=artifact.failure_policy,
                nodes=artifact.nodes,
                edges=artifact.edges,
                outputs=artifact.outputs,
                metadata=metadata,
            )
        except Exception as exc:
            raise PlanningError(
                "planner output normalization failed",
                code=ErrorCode.PLAN_TEMPLATE_INVALID,
                details={
                    "planner_id": planner_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc


class PlanMaterializer:
    """在 Harness-owned trust boundary 为模板分配一次 fresh execution identity。"""

    def __init__(self, identity_factory: PlanIdentityFactory | None = None) -> None:
        if identity_factory is not None and not isinstance(identity_factory, PlanIdentityFactory):
            raise TypeError("identity_factory must be PlanIdentityFactory")
        self._identity_factory = identity_factory or PlanIdentityFactory()

    @property
    def identity_factory(self) -> PlanIdentityFactory:
        return self._identity_factory

    def materialize(
        self,
        template: PlanTemplate | PlanDraft,
        invocation: InvocationContext,
        *,
        planner_id: str,
        trusted_metadata: Mapping[str, object] | None = None,
    ) -> ExecutionPlan:
        if isinstance(template, PlanDraft):
            template = PlanTemplate(
                budget=template.budget,
                failure_policy=template.failure_policy,
                nodes=template.nodes,
                edges=template.edges,
                outputs=template.outputs,
            )
        if not isinstance(template, PlanTemplate):
            raise TypeError("template must be PlanTemplate or PlanDraft")
        if not isinstance(invocation, InvocationContext):
            raise TypeError("invocation must be InvocationContext")
        if not isinstance(planner_id, str) or not planner_id.strip():
            raise TypeError("planner_id must be a non-empty string")
        if trusted_metadata is not None and not isinstance(trusted_metadata, Mapping):
            raise TypeError("trusted_metadata must be a mapping")

        try:
            plan_id = self._identity_factory.new_plan_id()
        except Exception as exc:
            raise PlanningError(
                "plan identity generation failed",
                code=ErrorCode.PLAN_IDENTITY_GENERATION_FAILED,
                details={
                    "planner_id": planner_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc

        metadata = dict(template.metadata)
        if trusted_metadata is not None:
            metadata.update(trusted_metadata)
        metadata.update(
            {
                "planner_id": planner_id,
                "request_id": invocation.request.request_id,
            }
        )
        return ExecutionPlan(
            plan_id=plan_id,
            revision=1,
            budget=template.budget,
            failure_policy=template.failure_policy,
            nodes=template.nodes,
            edges=template.edges,
            outputs=template.outputs,
            metadata=metadata,
        )

    def materialize_harness(
        self,
        template: PlanTemplate,
        invocation: InvocationContext,
        *,
        creator_id: str,
        trusted_metadata: Mapping[str, object] | None = None,
    ) -> ExecutionPlan:
        """为 Harness-owned 模板分配 identity，不把其伪装成 Planner 输出。"""

        if not isinstance(template, PlanTemplate):
            raise TypeError("template must be PlanTemplate")
        if not isinstance(invocation, InvocationContext):
            raise TypeError("invocation must be InvocationContext")
        if not isinstance(creator_id, str) or not creator_id.strip():
            raise TypeError("creator_id must be a non-empty string")
        if trusted_metadata is not None and not isinstance(trusted_metadata, Mapping):
            raise TypeError("trusted_metadata must be a mapping")
        try:
            plan_id = self._identity_factory.new_plan_id()
        except Exception as exc:
            raise PlanningError(
                "plan identity generation failed",
                code=ErrorCode.PLAN_IDENTITY_GENERATION_FAILED,
                details={"creator_id": creator_id, "cause_type": type(exc).__name__},
            ) from exc
        metadata = dict(template.metadata)
        if trusted_metadata is not None:
            metadata.update(trusted_metadata)
        metadata.update(
            {
                "creator_id": creator_id,
                "request_id": invocation.request.request_id,
            }
        )
        return ExecutionPlan(
            plan_id=plan_id,
            revision=1,
            budget=template.budget,
            failure_policy=template.failure_policy,
            nodes=template.nodes,
            edges=template.edges,
            outputs=template.outputs,
            metadata=metadata,
        )
