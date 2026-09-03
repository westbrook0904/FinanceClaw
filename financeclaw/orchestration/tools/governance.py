"""Tool 治理元数据：以声明式契约描述每个受治理 Tool 的风险与约束。

属于 orchestration/tools 治理层的基础模块，被目录、策略与各类 Tool
实现共同依赖；执行策略（ToolPolicy）依据这里的元数据做放行判定。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel import DataClassification


class SideEffect(StrEnum):
    """Tool 副作用类型枚举，标注调用会对世界产生哪类改变。

    使用场景：填写 ToolGovernance.side_effect；执行策略据此决定审批与
    重试资格，写入与外部动作类 Tool 被强制要求人工审批且禁止自动重试。
    """

    READ = "read"
    WRITE = "write"
    EXTERNAL_ACTION = "external_action"
    DELEGATION = "delegation"


class Idempotency(StrEnum):
    """Tool 幂等性枚举，声明重复调用同一请求时的行为保证。

    使用场景：填写 ToolGovernance.idempotency；KEY_REQUIRED 表示调用方
    必须携带幂等键才能安全重放，用于写入与委托类 Tool 的去重。
    """

    NONE = "none"
    IDEMPOTENT = "idempotent"
    KEY_REQUIRED = "key_required"


class RiskLevel(StrEnum):
    """Tool 风险等级枚举，从低到高刻画误用的潜在危害。

    使用场景：填写 ToolGovernance.risk_level，供审批与审计策略分级处理。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalMode(StrEnum):
    """Tool 执行前的人工审批模式枚举。

    使用场景：填写 ToolGovernance.approval；ALWAYS 表示每次执行前都要
    经过人在回路（HITL）批准，NONE 表示可信上下文下可直接执行。
    """

    NONE = "none"
    ALWAYS = "always"


class Egress(StrEnum):
    """Tool 数据出域范围枚举，声明数据可流向的边界。

    使用场景：填写 ToolGovernance.egress；EXTERNAL 表示数据可能离开
    平台边界，需要额外的出域管控与审计关注。
    """

    NONE = "none"
    INTERNAL = "internal"
    EXTERNAL = "external"


class Sensitivity(StrEnum):
    """Tool 可接触数据的敏感级别枚举，与数据分级体系对齐。

    使用场景：填写 ToolGovernance.sensitivity，作为 Tool 可见性与
    数据分级校验的依据之一。
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RetryProfile(StrEnum):
    """Tool 自动重试策略枚举，声明失败后能否自动重试及适用范围。

    使用场景：填写 ToolGovernance.retry_profile；TRANSIENT_READ 仅允许
    只读 Tool 对瞬态错误自动重试，写入与外部动作类 Tool 一律禁止。
    """

    NONE = "none"
    TRANSIENT_READ = "transient_read"


class AuditLevel(StrEnum):
    """Tool 审计记录的详细程度枚举。

    使用场景：填写 ToolGovernance.audit_level；FULL 记录决策与执行全
    过程，EXECUTION 仅记录执行，DECISION 仅记录放行决策。
    """

    DECISION = "decision"
    EXECUTION = "execution"
    FULL = "full"


class ToolGovernance(BaseModel):
    """单个受治理 Tool 的声明式治理契约：风险、审批、出域与数据分级约束。

    使用场景：每个 ManagedTool 构建时必须携带一份治理元数据；执行策略
    （ToolPolicy）据此判定放行、拒绝或要求审批，审计与目录消费这些
    字段做归因与版本管理。实例冻结且禁止未知字段，构建后不可篡改。

    Attributes:
        tool_id: Tool 唯一标识，1~128 字符，必须与 BaseTool.name 一致。
        version: Tool 语义化版本号，形如 ``major.minor.patch``。
        side_effect: 副作用类型，决定审批与重试约束。
        idempotency: 幂等性保证，写入类 Tool 通常要求幂等键。
        risk_level: 风险等级，供审批与审计分级。
        required_scopes: 调用该 Tool 所需的作用域集合，缺省为空集。
        approval: 人工审批模式；写入与外部动作类必须为 ALWAYS。
        egress: 数据出域范围。
        sensitivity: 可接触数据的敏感级别。
        retry_profile: 自动重试策略。
        audit_level: 审计详细程度，默认 FULL。
        direct_invocation: 是否允许绕过 Agent 经 API 直接调用，
            默认允许；委托类与记忆类 Tool 会显式关闭。
        tenant_allowlist: 允许使用该 Tool 的租户白名单；None 表示
            不限制租户。
        allowed_data_classes: 允许处理的数据密级集合，缺省放开全部
            密级，敏感 Tool 应显式收窄。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: Annotated[str, Field(min_length=1, max_length=128)]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    side_effect: SideEffect
    idempotency: Idempotency
    risk_level: RiskLevel
    required_scopes: frozenset[str] = Field(default_factory=frozenset)
    approval: ApprovalMode
    egress: Egress
    sensitivity: Sensitivity
    retry_profile: RetryProfile
    audit_level: AuditLevel = AuditLevel.FULL
    direct_invocation: bool = True
    tenant_allowlist: frozenset[str] | None = None
    allowed_data_classes: frozenset[DataClassification] = Field(
        default_factory=lambda: frozenset(DataClassification)
    )

    @model_validator(mode="after")
    def validate_safety_invariants(self) -> "ToolGovernance":
        """校验跨字段安全不变式，拒绝自相矛盾的治理配置。

        Returns:
            校验通过的原始实例。

        Raises:
            ValueError: 写入/外部动作类 Tool 未强制审批、允许自动重试，
                或瞬态重试配置用在了非只读 Tool 上。

        """
        # 1. 写入与外部动作类 Tool 必须强制人工审批。
        mutable_effects = {SideEffect.WRITE, SideEffect.EXTERNAL_ACTION}
        if self.side_effect in mutable_effects and self.approval is not ApprovalMode.ALWAYS:
            raise ValueError("WRITE and external-action tools must always require approval")
        # 2. 写入与外部动作类 Tool 禁止自动重试，避免重复产生副作用。
        if self.side_effect in mutable_effects and self.retry_profile is not RetryProfile.NONE:
            raise ValueError("WRITE and external-action tools cannot use automatic retry")
        # 3. 瞬态重试配置只允许出现在只读 Tool 上。
        if (
            self.retry_profile is RetryProfile.TRANSIENT_READ
            and self.side_effect is not SideEffect.READ
        ):
            raise ValueError("transient-read retry is only valid for READ tools")
        return self


@dataclass(frozen=True, slots=True)
class ManagedTool:
    """治理受管 Tool：把 LangChain Tool 实现与其治理元数据成对绑定。

    使用场景：各类 Tool 实现完成装配后都包装为 ManagedTool 再注册进
    ToolCatalog；执行策略与审计层通过 governance 字段做放行判定与
    归因，通过 tool 字段做实际调用。

    Attributes:
        tool: LangChain ``BaseTool`` 实例，承担实际的工具执行。
        governance: 该 Tool 的治理元数据，其 tool_id 必须与 tool.name
            一致。

    """

    tool: BaseTool
    governance: ToolGovernance

    def __post_init__(self) -> None:
        """校验绑定不变式：tool 必须是 BaseTool 且名称与治理 ID 一致。

        Raises:
            TypeError: tool 不是 LangChain ``BaseTool`` 实例。
            ValueError: tool.name 与 governance.tool_id 不一致。

        """
        if not isinstance(self.tool, BaseTool):
            raise TypeError("tool must be a LangChain BaseTool")
        if self.tool.name != self.governance.tool_id:
            raise ValueError("BaseTool name must match governance tool_id")

    @property
    def key(self) -> tuple[str, str]:
        """返回目录索引键 (tool_id, version)。"""
        return self.governance.tool_id, self.governance.version
