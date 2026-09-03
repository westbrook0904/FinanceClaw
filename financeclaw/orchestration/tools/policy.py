"""根据执行上下文和治理元数据作出工具授权、审批与重试判断。"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from financeclaw.kernel import ExecutionContext

from .governance import ApprovalMode, ManagedTool, RetryProfile, SideEffect, ToolGovernance


class ToolDecisionType(StrEnum):
    """策略引擎对一次工具调用作出的决策类型。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        ALLOW: 策略允许本次调用立即执行。
        DENY: 策略拒绝本次调用。
        REQUIRE_APPROVAL: 策略要求在执行前取得人工批准。
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ToolDecision(BaseModel):
    """定义工具决策。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        effect: 策略决策结果：允许、拒绝或要求审批。
        reason: 产生当前决策、遗漏或状态的可读原因。
        policy_version: 作出决策时使用的策略版本。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: ToolDecisionType
    reason: str
    policy_version: str


class TransientToolError(ConnectionError):
    """定义Transient工具Error。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """


class ToolPolicy:
    """结合调用者权限与工具元数据返回允许、拒绝或需审批决策。

    适用场景：
        用于在执行副作用前作出确定性治理决策。

    属性：
        version: 语义版本，用于固定运行行为并支持审计复现。
    """

    version = "tool-policy/1.0.0"

    def evaluate(
        self,
        context: ExecutionContext,
        governance: ToolGovernance,
        arguments: dict[str, Any],
    ) -> ToolDecision:
        """依次检查租户、权限域和审批规则，返回可审计的确定性决策。"""
        del arguments
        if (
            governance.tenant_allowlist is not None
            and context.tenant_id not in governance.tenant_allowlist
        ):
            return ToolDecision(
                effect=ToolDecisionType.DENY,
                reason="tenant is not allowed to use this tool",
                policy_version=self.version,
            )
        granted = context.scopes
        required = governance.required_scopes
        if "*" not in granted and not required.issubset(granted):
            return ToolDecision(
                effect=ToolDecisionType.DENY,
                reason="required tool scope is missing",
                policy_version=self.version,
            )
        if context.data_classification not in governance.allowed_data_classes:
            return ToolDecision(
                effect=ToolDecisionType.DENY,
                reason="tool egress does not allow this data classification",
                policy_version=self.version,
            )
        if governance.approval is ApprovalMode.ALWAYS:
            return ToolDecision(
                effect=ToolDecisionType.REQUIRE_APPROVAL,
                reason="tool governance requires human approval",
                policy_version=self.version,
            )
        return ToolDecision(
            effect=ToolDecisionType.ALLOW,
            reason="trusted context satisfies tool governance",
            policy_version=self.version,
        )

    def visible(self, context: ExecutionContext, managed: ManagedTool) -> bool:
        """判断工具是否应出现在当前调用主体可见的模型工具集合中。"""
        return self.evaluate(context, managed.governance, {}).effect is not ToolDecisionType.DENY

    def retryable(
        self,
        governance: ToolGovernance,
        error: Exception,
        arguments: dict[str, Any],
    ) -> bool:
        """仅允许声明为瞬时读取且满足幂等条件的失败重试。"""
        del arguments
        return (
            governance.side_effect is SideEffect.READ
            and governance.retry_profile is RetryProfile.TRANSIENT_READ
            and isinstance(error, TransientToolError)
        )
