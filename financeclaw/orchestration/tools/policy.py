"""Tool 执行策略：依据治理元数据与执行上下文做出放行、审批与重试判定。

属于 orchestration/tools 治理层的决策模块，在 Tool 执行前由编排层调用
evaluate 做准入判定，并决定写类 Tool 是否需要人工二次授权（HITL）。
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from financeclaw.kernel import ExecutionContext

from .governance import ApprovalMode, ManagedTool, RetryProfile, SideEffect, ToolGovernance


class ToolDecisionType(StrEnum):
    """Tool 准入决策类型枚举：允许、拒绝或要求人工审批。

    使用场景：ToolPolicy.evaluate 的输出效果值；REQUIRE_APPROVAL 会
    触发在回路（HITL）中断，等待用户批准后再继续执行。
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ToolDecision(BaseModel):
    """一次 Tool 准入判定的结论记录：效果、原因与策略版本。

    使用场景：ToolPolicy.evaluate 的返回值；编排层据此决定放行、
    中断审批或拒绝，结论连同 policy_version 一并写入审计便于复现。

    Attributes:
        effect: 决策效果，见 ToolDecisionType。
        reason: 机器可读的决策原因，供审计与错误响应使用。
        policy_version: 作出判定时使用的策略版本号，用于审计复现。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: ToolDecisionType
    reason: str
    policy_version: str


class TransientToolError(ConnectionError):
    """可自动重试的瞬态 Tool 错误，表示外部依赖暂时不可用。

    使用场景：只读 Tool（如行情读取）在依赖短暂故障时抛出；
    ToolPolicy.retryable 只对该类错误且治理配置允许时自动重试。
    """

    pass


class ToolPolicy:
    """受治理 Tool 的执行策略：统一做租户、作用域、数据分级与审批判定。

    使用场景：编排层在每次 Tool 调用前调用 evaluate 取得准入决策，
    在写类 Tool 上触发人工二次授权；装配 Tool 清单时用 visible 过滤
    当前上下文不可见的 Tool，失败重试时用 retryable 判定是否自动重试。

    Attributes:
        version: 策略语义版本号，随每次判定结果写入审计。

    """

    version = "tool-policy/1.0.0"

    def evaluate(
        self,
        context: ExecutionContext,
        governance: ToolGovernance,
        arguments: dict[str, Any],
    ) -> ToolDecision:
        """按租户白名单、作用域、数据分级与审批要求依次判定准入结论。

        Args:
            context: 当前执行上下文，提供租户、作用域与数据密级。
            governance: 目标 Tool 的治理元数据。
            arguments: 本次调用的入参；当前策略未使用，保留以兼容
                未来基于参数的细化规则。

        Returns:
            携带效果、原因与策略版本的准入决策。

        """
        del arguments
        # 1. 校验租户白名单：配置了白名单且当前租户不在其中则直接拒绝。
        if (
            governance.tenant_allowlist is not None
            and context.tenant_id not in governance.tenant_allowlist
        ):
            return ToolDecision(
                effect=ToolDecisionType.DENY,
                reason="tenant is not allowed to use this tool",
                policy_version=self.version,
            )
        # 2. 校验作用域：持有通配 "*" 时豁免，否则要求覆盖全部所需 scope。
        granted = context.scopes
        required = governance.required_scopes
        if "*" not in granted and not required.issubset(granted):
            return ToolDecision(
                effect=ToolDecisionType.DENY,
                reason="required tool scope is missing",
                policy_version=self.version,
            )
        # 3. 校验数据分级：本次运行密级必须落在 Tool 允许的密级集合内。
        if context.data_classification not in governance.allowed_data_classes:
            return ToolDecision(
                effect=ToolDecisionType.DENY,
                reason="tool egress does not allow this data classification",
                policy_version=self.version,
            )
        # 4. 写类 Tool 触发人工审批，其余可信上下文直接放行。
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
        """判定某个受治理 Tool 在当前上下文下是否对调用方可见。

        Args:
            context: 当前执行上下文。
            managed: 待检查的受治理 Tool。

        Returns:
            准入决策不是 DENY 时返回 True；要求审批的 Tool 依然可见。

        """
        return self.evaluate(context, managed.governance, {}).effect is not ToolDecisionType.DENY

    def retryable(
        self,
        governance: ToolGovernance,
        error: Exception,
        arguments: dict[str, Any],
    ) -> bool:
        """判定一次失败的 Tool 调用能否自动重试。

        仅当 Tool 是只读、治理配置允许瞬态重试且错误是瞬态错误时才
        允许自动重试，写入类 Tool 永不自动重试。

        Args:
            governance: 目标 Tool 的治理元数据。
            error: 本次调用抛出的异常。
            arguments: 失败调用的入参；当前策略未使用，保留以兼容
                未来基于参数的细化规则。

        Returns:
            允许自动重试时返回 True。

        """
        del arguments
        return (
            governance.side_effect is SideEffect.READ
            and governance.retry_profile is RetryProfile.TRANSIENT_READ
            and isinstance(error, TransientToolError)
        )
