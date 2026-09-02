"""Small deterministic Tool authorization and retry policy."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from financeclaw.contracts import ExecutionContext

from .governance import ApprovalMode, ManagedTool, RetryProfile, SideEffect, ToolGovernance


class ToolDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: ToolDecisionType
    reason: str
    policy_version: str


class TransientToolError(ConnectionError):
    """Explicitly retryable upstream failure for a READ tool."""


class ToolPolicy:
    """Finance-specific authorization without a registry or rules language."""

    version = "tool-policy/1.0.0"

    def evaluate(
        self,
        context: ExecutionContext,
        governance: ToolGovernance,
        arguments: dict[str, Any],
    ) -> ToolDecision:
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
        return self.evaluate(context, managed.governance, {}).effect is not ToolDecisionType.DENY

    def retryable(
        self,
        governance: ToolGovernance,
        error: Exception,
        arguments: dict[str, Any],
    ) -> bool:
        del arguments
        return (
            governance.side_effect is SideEffect.READ
            and governance.retry_profile is RetryProfile.TRANSIENT_READ
            and isinstance(error, TransientToolError)
        )
