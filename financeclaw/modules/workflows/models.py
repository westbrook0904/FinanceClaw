"""工作流的定义、运行与审批领域模型，以及对应的生命周期状态枚举。

WorkflowDefinition 固定一次流程装配的全部要素；WorkflowRun 与
WorkflowApproval 记录运行与 HITL 审批的事实，由 BFF 永久保存。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenWorkflowModel(BaseModel):
    """所有工作流模型共用的不可变 Pydantic 基类。

    使用场景：
        保证运行、审批等事实记录在创建后不可被篡改或随意扩展。

    Attributes:
        model_config: Pydantic 配置；禁止未知字段（extra="forbid"）并冻结实例。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowStatus(StrEnum):
    """工作流定义的发布状态。

    使用场景：
        目录只解析 ACTIVE 定义；DEPRECATED 版本保留可查但不可再启动。

    Attributes:
        DRAFT: 尚未发布，仅用于编辑与评审。
        ACTIVE: 已发布，可被目录解析并启动运行。
        DEPRECATED: 已废弃，保留定义但不再允许新运行。

    """

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class WorkflowRunStatus(StrEnum):
    """工作流运行的生命周期状态。

    使用场景：
        驱动 BFF 侧的运行状态机；COMPLETED/REJECTED/FAILED 为终态，不可再变更。

    Attributes:
        ACCEPTED: 请求已受理，尚未在 Agent Server 上启动。
        PENDING: 运行已提交，等待执行或审批。
        RUNNING: 正在图中执行。
        INTERRUPTED: 停在审批等 interrupt 检查点，等待恢复。
        COMPLETED: 运行成功结束。
        REJECTED: 审批被拒绝，运行终止。
        FAILED: 执行失败，运行终止。

    """

    ACCEPTED = "accepted"
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class WorkflowApprovalStatus(StrEnum):
    """人工审批决定的生命周期状态。

    使用场景：
        审批点产生 PENDING 记录，复验通过后由审批人落成终态。

    Attributes:
        PENDING: 等待审批人决定。
        APPROVED: 已批准，运行可恢复。
        REJECTED: 已拒绝，运行终止。
        EXPIRED: 超过审批时限，未再被决定。

    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class WorkflowToolRef(FrozenWorkflowModel):
    """工作流允许使用的工具及其固定版本引用。

    使用场景：
        装配期把流程绑定的工具版本固化进定义，运行期据此校验可用工具。

    Attributes:
        tool_id: 工具稳定标识，1 到 128 字符。
        version: 工具语义化版本，形如 x.y.z。

    """

    tool_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ApprovalPoint(FrozenWorkflowModel):
    """工作流中需要人工审批（HITL）的检查点定义。

    使用场景：
        图执行到该检查点时通过 LangGraph interrupt 暂停，BFF 侧据此
        生成审批请求，恢复前复验权限、归属与原始参数哈希。

    Attributes:
        approval_id: 检查点稳定标识，在同一工作流内唯一。
        description: 面向审批人的检查点说明，1 到 500 字符。
        requested_action: 该检查点请求确认的具体动作。
        allowed_decisions: 允许的决定集合，默认仅 approve 与 reject。
        required_scope: 作出决定所需的权限域，默认 workflows:approve。

    """

    approval_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    requested_action: str = Field(min_length=1, max_length=128)
    allowed_decisions: tuple[Literal["approve", "reject"], ...] = ("approve", "reject")
    required_scope: str = "workflows:approve"

    @model_validator(mode="after")
    def decisions_are_nonempty_and_unique(self) -> ApprovalPoint:
        """校验决定集合非空且取值不重复。"""
        if not self.allowed_decisions or len(self.allowed_decisions) != len(
            set(self.allowed_decisions)
        ):
            raise ValueError("approval decisions must be nonempty and unique")
        return self


class WorkflowTimeoutPolicy(FrozenWorkflowModel):
    """工作流运行与审批的超时策略。

    使用场景：
        装配期固化超时参数，随运行快照持久化，用于运行超时与审批过期判定。

    Attributes:
        run_timeout_seconds: 单次运行允许的最长时长（秒），默认 300。
        approval_timeout_seconds: 审批等待的最长时限（秒），默认 900。

    """

    run_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    approval_timeout_seconds: int = Field(default=900, ge=30, le=604_800)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """一个已发布工作流的不可变定义，固定其全部装配要素。

    使用场景：
        在启动期由目录装配登记；启动运行时按版本取出，归一化输入，
        并绑定图、模型档案、工具版本、审批点与超时策略。

    Attributes:
        workflow_id: 工作流稳定标识，如 portfolio_review。
        version: 语义化版本号，形如 x.y.z。
        assistant_id: Agent Server 侧承载该流程的助手标识。
        graph: 编译后的 LangGraph 图对象；发布定义不允许为空。
        input_schema: 输入参数的 Pydantic 模型类型，用于校验与归一化。
        output_schema: 输出结果的 Pydantic 模型类型。
        model_profile_id: 本次流程固定使用的模型档案标识。
        allowed_tools: 允许使用的工具版本集合，（工具，版本）组合不重复。
        approval_points: 人工审批检查点集合，检查点标识不重复。
        timeout_policy: 运行与审批的超时策略。
        status: 发布状态，仅 ACTIVE 可被目录解析。
        deployment_revision: 装配该定义时的部署修订号，用于定位运行代码。
        required_scopes: 启动该流程所需的权限域集合。

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
        """校验标识、版本、工具与审批点等装配期不变量。"""
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
        """返回目录索引键（workflow_id, version）。"""
        return self.workflow_id, self.version

    def normalize_input(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按输入模式校验并归一化参数，返回 JSON 兼容字典。

        Args:
            arguments: 调用方提交的原始参数。

        Returns:
            经 input_schema 校验后的 JSON 兼容参数字典。

        """
        return self.input_schema.model_validate(arguments).model_dump(mode="json")


class WorkflowRun(FrozenWorkflowModel):
    """一次工作流运行的持久事实记录，由 BFF 永久保存。

    使用场景：
        串起客户端请求、Workflow 独占 thread 与 server run 的映射，
        并保存输入哈希、输出与发布制品引用以支撑追溯与审计。

    Attributes:
        run_id: 应用侧运行标识。
        tenant_id: 租户隔离键。
        subject_id: 已认证主体标识，用于所有权校验。
        workflow_id: 本次运行的工作流标识。
        workflow_version: 本次运行固定的工作流版本。
        assistant_id: Agent Server 侧助手标识。
        deployment_revision: 装配该运行所用的部署修订号。
        model_profile_id: 本次运行固定的模型档案标识。
        run_timeout_seconds: 运行超时快照（秒）。
        approval_timeout_seconds: 审批超时快照（秒）。
        thread_id: Workflow 独占的 Agent Server thread 标识。
        server_run_id: 绑定的 Agent Server 运行标识；尚未绑定时为空。
        client_idempotency_key: 客户端幂等键，与租户、流程、版本共同唯一。
        arguments_hash: 规范化输入参数的 SHA-256，审批恢复前用于复验。
        request_fingerprint: 完整请求的 SHA-256 指纹，用于幂等冲突检测。
        input_payload: 归一化后的输入参数快照。
        output_payload: 终态时的结构化输出快照；未结束时为空。
        artifact_refs: 本次运行发布的制品标识（如审批后的报告）。
        status: 当前运行状态。
        started_at: 运行创建时间。
        updated_at: 最近一次状态变更时间。
        completed_at: 进入终态的时间；未结束时为空。

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
    """一次人工审批的持久事实记录，与 LangGraph interrupt 检查点对应。

    使用场景：
        运行停在审批点时创建 PENDING 记录；恢复前复验权限、归属、
        原始参数哈希与过期时间，决定后再落成终态。

    Attributes:
        approval_id: 审批请求稳定标识。
        run_id: 关联的工作流运行标识。
        tenant_id: 租户隔离键。
        subject_id: 发起运行的主体标识。
        approval_point: 触发审批的检查点标识，同一运行内唯一。
        arguments_hash: 绑定的输入参数哈希，恢复前用于篡改检测。
        requested_action: 请求人工确认的具体动作。
        request_payload: 展示给审批人的请求参数快照。
        allowed_decisions: 允许的决定值集合。
        required_scope: 作出决定所需的权限域。
        status: 审批状态。
        requested_at: 审批请求创建时间。
        expires_at: 审批过期时间，超时后不得再决定。
        decided_at: 决定时间；未决定时为空。
        decided_by: 作出决定的主体标识；未决定时为空。
        decision_reason: 审批人给出的决定理由；可为空。

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
