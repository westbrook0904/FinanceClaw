"""定义工作流输入目标、运行状态、审批和版本化定义。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenWorkflowModel(BaseModel):
    """定义不可变工作流模型。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowStatus(StrEnum):
    """定义工作流状态。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        DRAFT: 表示 `draft` 这一受限枚举值。
        ACTIVE: 记录当前有效，可继续读取或追加操作。
        DEPRECATED: 表示 `deprecated` 这一受限枚举值。
    """

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class WorkflowRunStatus(StrEnum):
    """工作流运行从接收到终态的生命周期状态。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        ACCEPTED: 表示 `accepted` 这一受限枚举值。
        PENDING: 操作已创建但尚未开始处理。
        RUNNING: 操作正在执行且尚未产生终态结果。
        INTERRUPTED: 运行停在可恢复检查点，等待外部决定。
        COMPLETED: 操作已成功完成并可读取最终结果。
        REJECTED: 表示 `rejected` 这一受限枚举值。
        FAILED: 操作已失败，错误信息应记录在对应字段。
    """

    ACCEPTED = "accepted"
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class WorkflowApprovalStatus(StrEnum):
    """定义工作流审批状态。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        PENDING: 操作已创建但尚未开始处理。
        APPROVED: 表示 `approved` 这一受限枚举值。
        REJECTED: 表示 `rejected` 这一受限枚举值。
        EXPIRED: 表示 `expired` 这一受限枚举值。
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class WorkflowToolRef(FrozenWorkflowModel):
    """定义工作流工具Ref。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        tool_id: 工具的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
    """

    tool_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ApprovalPoint(FrozenWorkflowModel):
    """定义审批Point。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        approval_id: 审批请求稳定标识。
        description: 供调用者、模型或运维人员理解用途的可读说明。
        requested_action: 需要人工确认的具体操作。
        allowed_decisions: 当前配置明确允许的值集合。
        required_scope: 作出该审批决定所需的权限域。
    """

    approval_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    requested_action: str = Field(min_length=1, max_length=128)
    allowed_decisions: tuple[Literal["approve", "reject"], ...] = ("approve", "reject")
    required_scope: str = "workflows:approve"

    @model_validator(mode="after")
    def decisions_are_nonempty_and_unique(self) -> ApprovalPoint:
        """校验审批Point的跨字段一致性；不满足不变量时拒绝构造。"""
        if not self.allowed_decisions or len(self.allowed_decisions) != len(
            set(self.allowed_decisions)
        ):
            raise ValueError("approval decisions must be nonempty and unique")
        return self


class WorkflowTimeoutPolicy(FrozenWorkflowModel):
    """定义工作流Timeout策略。

    适用场景：
        用于在执行副作用前作出确定性治理决策。

    属性：
        run_timeout_seconds: 该操作允许的最长时间（秒）。
        approval_timeout_seconds: 该操作允许的最长时间（秒）。
    """

    run_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    approval_timeout_seconds: int = Field(default=900, ge=30, le=604_800)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """定义工作流构建器及其权限、超时和版本元数据。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        workflow_id: 工作流的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        assistant_id: 提交 Agent Server 时使用的助手或图标识。
        graph: 已经编译、可由 Agent Server 执行的 LangGraph 图。
        input_schema: 工作流公开输入使用的 Pydantic 校验模型。
        output_schema: 工作流终态输出使用的 Pydantic 校验模型。
        model_profile_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        allowed_tools: 当前配置明确允许的值集合。
        approval_points: 该工作流定义允许产生中断的节点集合。
        timeout_policy: 工作流运行超时后采用的失败处理策略。
        status: 当前生命周期状态，决定记录允许的后续操作。
        deployment_revision: 构建工作流图的部署修订号，用于定位实际运行代码。
        required_scopes: 执行目标必须具备的权限域集合。
    """

    workflow_id: str
    version: str
    assistant_id: str
    graph: Any
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    model_profile_id: str
    allowed_tools: tuple[WorkflowToolRef, ...]
    approval_points: tuple[ApprovalPoint, ...]
    timeout_policy: WorkflowTimeoutPolicy
    status: WorkflowStatus
    deployment_revision: str
    required_scopes: frozenset[str]

    def __post_init__(self) -> None:
        """校验成对封装的工具标识与治理元数据完全一致。"""
        if not self.workflow_id or not self.assistant_id or not self.deployment_revision:
            raise ValueError("workflow identifiers cannot be empty")
        parts = self.version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError("workflow version must use semantic x.y.z form")
        if self.graph is None:
            raise ValueError("published workflow requires a compiled graph")
        tool_keys = tuple((item.tool_id, item.version) for item in self.allowed_tools)
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError("workflow allowed tool versions must be unique")
        approval_ids = tuple(item.approval_id for item in self.approval_points)
        if len(approval_ids) != len(set(approval_ids)):
            raise ValueError("workflow approval point IDs must be unique")

    @property
    def key(self) -> tuple[str, str]:
        """返回由稳定标识与版本组成的目录复合键。"""
        return self.workflow_id, self.version

    def normalize_input(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """将输入规范化为可比较、可持久化的工作流构建器及其权限、超时和版本元数据。"""
        return self.input_schema.model_validate(arguments).model_dump(mode="json")


class WorkflowRun(FrozenWorkflowModel):
    """定义一次工作流执行的持久化状态与服务端关联。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        workflow_id: 工作流的稳定标识。
        workflow_version: 本次运行固定使用的工作流版本。
        assistant_id: 提交 Agent Server 时使用的助手或图标识。
        deployment_revision: 构建工作流图的部署修订号，用于定位实际运行代码。
        model_profile_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        run_timeout_seconds: 该操作允许的最长时间（秒）。
        approval_timeout_seconds: 该操作允许的最长时间（秒）。
        thread_id: Agent Server 线程标识，用于保存运行检查点与消息状态。
        server_run_id: Agent Server 侧运行标识；尚未提交远端运行时为空。
        client_idempotency_key: 客户端幂等键，在同一资源范围内唯一。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        request_fingerprint: 委派或工作流请求的稳定指纹，用于幂等冲突检测。
        input_payload: 提交给工作流的规范化输入快照。
        output_payload: 运行终态时保存的结构化输出快照。
        artifact_refs: 本次运行、审计或事件关联的制品标识集合。
        status: 当前生命周期状态，决定记录允许的后续操作。
        started_at: 该生命周期事件发生的 UTC 时间。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
        completed_at: 进入成功或失败终态的时间；未结束时为空。
    """

    run_id: str
    tenant_id: str
    subject_id: str
    workflow_id: str
    workflow_version: str
    assistant_id: str
    deployment_revision: str
    model_profile_id: str
    run_timeout_seconds: int = Field(ge=1)
    approval_timeout_seconds: int = Field(ge=1)
    thread_id: str
    server_run_id: str | None = None
    client_idempotency_key: str
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_payload: dict[str, Any]
    output_payload: dict[str, Any] | None = None
    artifact_refs: tuple[str, ...] = ()
    status: WorkflowRunStatus
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class WorkflowApproval(FrozenWorkflowModel):
    """定义工作流中断点对应的人工审批快照。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        approval_id: 审批请求稳定标识。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        approval_point: 工作流中触发本次人工确认的稳定节点名称。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        requested_action: 需要人工确认的具体操作。
        request_payload: 审批点展示并绑定哈希的请求参数快照。
        allowed_decisions: 当前配置明确允许的值集合。
        required_scope: 作出该审批决定所需的权限域。
        status: 当前生命周期状态，决定记录允许的后续操作。
        requested_at: 该生命周期事件发生的 UTC 时间。
        expires_at: 记录或审批失效时间；为空表示不按时间自动失效。
        decided_at: 该生命周期事件发生的 UTC 时间。
        decided_by: 作出审批决定的主体标识。
        decision_reason: 审批人或策略给出的决定理由。
    """

    approval_id: str
    run_id: str
    tenant_id: str
    subject_id: str
    approval_point: str
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_action: str
    request_payload: dict[str, Any]
    allowed_decisions: tuple[str, ...]
    required_scope: str
    status: WorkflowApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
