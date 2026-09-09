"""Workflow 发布声明、运行与审批契约；编译图仅存在于执行端。"""

from __future__ import annotations

from dataclasses import dataclass
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
class WorkflowRelease:
    """一个已发布工作流的不可变声明，固定跨服务可见的执行契约。

    使用场景：
        在启动期由目录装配登记；启动运行时按版本取出，归一化输入，
        并绑定 assistant、模型档案、工具版本、审批点与超时策略。

    Attributes:
        workflow_id: 工作流稳定标识，如 portfolio_review。
        version: 语义化版本号，形如 x.y.z。
        assistant_id: Agent Server 侧承载该流程的助手标识。
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
