"""定义工具副作用、风险、审批、出站和审计策略元数据。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel import DataClassification


class SideEffect(StrEnum):
    """工具调用对外部状态产生的副作用类型。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        READ: 工具只读取数据，不应改变外部状态。
        WRITE: 工具会创建或修改外部持久化状态。
        EXTERNAL_ACTION: 工具会触发现实世界或第三方系统动作。
        DELEGATION: 工具把任务移交给另一个 Agent 或工作流。
    """

    READ = "read"
    WRITE = "write"
    EXTERNAL_ACTION = "external_action"
    DELEGATION = "delegation"


class Idempotency(StrEnum):
    """工具是否支持重试以及是否强制提供幂等键。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        NONE: 不启用该治理能力或没有对应副作用。
        IDEMPOTENT: 相同参数可安全重复执行并得到等价效果。
        KEY_REQUIRED: 只有携带稳定幂等键时才允许安全重试。
    """

    NONE = "none"
    IDEMPOTENT = "idempotent"
    KEY_REQUIRED = "key_required"


class RiskLevel(StrEnum):
    """工具调用的业务风险等级。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        LOW: 低风险，满足权限后通常可直接执行。
        MEDIUM: 中等风险，需要更严格审计或按策略审批。
        HIGH: 高风险，应在执行前取得明确人工审批。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalMode(StrEnum):
    """工具在执行前是否必须取得人工批准。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        NONE: 不启用该治理能力或没有对应副作用。
        ALWAYS: 每次执行都必须经过人工审批。
    """

    NONE = "none"
    ALWAYS = "always"


class Egress(StrEnum):
    """工具调用是否访问内部或外部网络。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        NONE: 不启用该治理能力或没有对应副作用。
        INTERNAL: 仅允许访问平台内部网络资源或处理内部级数据。
        EXTERNAL: 允许访问经出站策略批准的外部服务。
    """

    NONE = "none"
    INTERNAL = "internal"
    EXTERNAL = "external"


class Sensitivity(StrEnum):
    """工具可处理数据的最高敏感级别。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        PUBLIC: 无需访问控制即可公开的数据等级。
        INTERNAL: 仅允许访问平台内部网络资源或处理内部级数据。
        CONFIDENTIAL: 需要严格访问控制的机密数据等级。
        RESTRICTED: 受最严格限制、通常不得发送给外部供应方的数据等级。
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RetryProfile(StrEnum):
    """工具失败后可采用的重试策略。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        NONE: 不启用该治理能力或没有对应副作用。
        TRANSIENT_READ: 仅对瞬时读取错误采用受限重试。
    """

    NONE = "none"
    TRANSIENT_READ = "transient_read"


class AuditLevel(StrEnum):
    """工具调用需记录的审计详细程度。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        DECISION: 只记录治理决策及其关键依据。
        EXECUTION: 同时记录治理决策与执行结果。
        FULL: 记录经脱敏的完整决策、输入与执行结果。
    """

    DECISION = "decision"
    EXECUTION = "execution"
    FULL = "full"


class ToolGovernance(BaseModel):
    """定义一个工具版本的静态安全与合规约束。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        tool_id: 工具的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        side_effect: 调用对外部状态的影响类别。
        idempotency: 调用的幂等能力及幂等键要求。
        risk_level: 调用风险等级，用于决定审批与审计强度。
        required_scopes: 执行目标必须具备的权限域集合。
        approval: 执行前采用的人工审批策略。
        egress: 调用所需的网络出站范围。
        sensitivity: 允许处理的数据敏感级别。
        retry_profile: 失败后允许采用的重试策略。
        audit_level: 授权与执行过程要求的审计粒度。
        direct_invocation: 是否允许调用方绕过 Agent 规划直接执行该工具。
        tenant_allowlist: 可使用该工具的租户白名单；为空表示不额外限制。
        allowed_data_classes: 该配置允许发送或处理的数据分类集合。
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
        """校验一个工具版本的静态安全与合规约束的跨字段一致性；不满足不变量时拒绝构造。"""
        mutable_effects = {SideEffect.WRITE, SideEffect.EXTERNAL_ACTION}
        if self.side_effect in mutable_effects and self.approval is not ApprovalMode.ALWAYS:
            raise ValueError("WRITE and external-action tools must always require approval")
        if self.side_effect in mutable_effects and self.retry_profile is not RetryProfile.NONE:
            raise ValueError("WRITE and external-action tools cannot use automatic retry")
        if (
            self.retry_profile is RetryProfile.TRANSIENT_READ
            and self.side_effect is not SideEffect.READ
        ):
            raise ValueError("transient-read retry is only valid for READ tools")
        return self


@dataclass(frozen=True, slots=True)
class ManagedTool:
    """定义LangChain 工具及其不可分离的治理元数据。

    适用场景：
        用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。

    属性：
        tool: 实际执行能力的 LangChain 工具实例。
        governance: 与工具版本绑定的静态治理元数据。
    """

    tool: BaseTool
    governance: ToolGovernance

    def __post_init__(self) -> None:
        """校验成对封装的工具标识与治理元数据完全一致。"""
        if not isinstance(self.tool, BaseTool):
            raise TypeError("tool must be a LangChain BaseTool")
        if self.tool.name != self.governance.tool_id:
            raise ValueError("BaseTool name must match governance tool_id")

    @property
    def key(self) -> tuple[str, str]:
        """返回由稳定标识与版本组成的目录复合键。"""
        return self.governance.tool_id, self.governance.version
