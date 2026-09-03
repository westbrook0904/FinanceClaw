"""Versioned governance metadata attached to LangChain tools."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.contracts import DataClassification


class SideEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    EXTERNAL_ACTION = "external_action"
    DELEGATION = "delegation"


class Idempotency(StrEnum):
    NONE = "none"
    IDEMPOTENT = "idempotent"
    KEY_REQUIRED = "key_required"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalMode(StrEnum):
    NONE = "none"
    ALWAYS = "always"


class Egress(StrEnum):
    NONE = "none"
    INTERNAL = "internal"
    EXTERNAL = "external"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RetryProfile(StrEnum):
    NONE = "none"
    TRANSIENT_READ = "transient_read"


class AuditLevel(StrEnum):
    DECISION = "decision"
    EXECUTION = "execution"
    FULL = "full"


class ToolGovernance(BaseModel):
    """Locally authoritative policy metadata; remote schemas cannot override it."""

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
    tool: BaseTool
    governance: ToolGovernance

    def __post_init__(self) -> None:
        if not isinstance(self.tool, BaseTool):
            raise TypeError("tool must be a LangChain BaseTool")
        if self.tool.name != self.governance.tool_id:
            raise ValueError("BaseTool name must match governance tool_id")

    @property
    def key(self) -> tuple[str, str]:
        return self.governance.tool_id, self.governance.version
