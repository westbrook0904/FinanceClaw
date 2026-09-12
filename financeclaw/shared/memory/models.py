"""Owner-bound, immutable contracts for durable memory facts and decisions."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel.context import DataClassification


class MemoryContract(BaseModel):
    """Reject unexpected model output and keep transaction snapshots immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class ProfileField(StrEnum):
    """Registered profile fields with deliberately different confirmation rules."""

    LANGUAGE = "language"
    VERBOSITY = "verbosity"
    OUTPUT_FORMAT = "output_format"
    INVESTMENT_GOAL = "investment_goal"
    RISK_STATEMENT = "risk_statement"
    ACCOUNT_SCOPE = "account_scope"
    CONSTRAINT = "constraint"


class MemoryActor(MemoryContract):
    """Trusted adapter identity; never deserialize this contract from a public body."""

    tenant_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=128)
    kind: Literal["user", "tool", "worker"] = "user"
    scopes: frozenset[str] = frozenset()
    turn_id: str | None = None
    conversation_id: str | None = None
    tool_call_id: str | None = None
    agent_id: str | None = None
    permit_source_ids: tuple[str, ...] = ()
    data_classification: DataClassification = DataClassification.INTERNAL
    processing_region: str = Field(default="global", min_length=1, max_length=64)


class EvidenceRef(MemoryContract):
    """An exact source version and optional verified original character range."""

    source_id: str
    source_kind: str
    source_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_seq: int = Field(ge=1)
    span: tuple[int, int] | None = None


class MemoryDerivationPermit(MemoryContract):
    """Finite source-bound authority, independent of a Turn's natural expiry."""

    tenant_id: str
    subject_id: str
    source_id: str
    source_version: int
    content_hash: str
    policy_revision: int
    authorization_hash: str
    purpose: Literal["derive_memory"] = "derive_memory"
    data_classification: DataClassification = DataClassification.INTERNAL
    processing_region: str = Field(default="global", min_length=1, max_length=64)
    expires_at: datetime
    max_input_tokens: int = 24000
    max_output_tokens: int = 2000
    max_consolidation_output_tokens: int = 4000


class MemorySource(MemoryContract):
    """Reference-only source directory snapshot, without duplicated user text."""

    source_id: str
    tenant_id: str
    subject_id: str
    source_seq: int
    source_kind: str
    object_id: str
    source_version: int
    content_hash: str
    conversation_id: str | None = None
    turn_id: str | None = None
    visible: bool = True
    version_valid: bool = True
    reuse_blocked: bool = False
    permit: MemoryDerivationPermit | None = None
    permit_revoked: bool = False
    data_classification: DataClassification = DataClassification.INTERNAL
    processing_region: str = Field(default="global", min_length=1, max_length=64)

    def evidence_ref(self) -> EvidenceRef:
        """Return the complete immutable evidence identity for model proposals."""
        return EvidenceRef(
            **{name: getattr(self, name) for name in EvidenceRef.model_fields if name != "span"}
        )


class MemoryMutation(MemoryContract):
    """A proposal whose actor, evidence and concurrency predicates are server checked."""

    mutation_id: str = Field(min_length=1, max_length=256)
    operation: Literal["create", "update", "forget"] = "create"
    memory_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    kind: Literal["profile", "task"] = "task"
    scope_type: Literal["user", "agent", "conversation"] = "user"
    scope_id: str = Field(default="", max_length=128)
    field: ProfileField | None = None
    content: str = Field(default="", max_length=12000)
    evidence: tuple[EvidenceRef, ...] = Field(default=(), max_length=128)
    explicit_intent: bool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        """Reject empty facts, ambiguous targets and unregistered profile shapes."""
        if self.operation != "forget" and not self.content.strip():
            raise ValueError("memory content must not be empty")
        if self.operation in {"update", "forget"} and (
            not self.memory_id or self.expected_revision is None
        ):
            raise ValueError("update and forget require target and expected revision")
        if self.kind == "profile" and self.operation != "forget" and self.field is None:
            raise ValueError("profile requires a registered field")
        if self.kind == "task" and self.field is not None:
            raise ValueError("task memory cannot carry a profile field")
        if (self.scope_type == "user") != (self.scope_id == ""):
            raise ValueError("only user scope has an empty scope ID")
        return self


class MemoryReceipt(MemoryContract):
    """Permanent minimal result; historical replay must never disclose old text."""

    status: Literal["committed", "proposed", "forgotten", "rejected"]
    memory_id: str
    revision: int
    candidate_id: str | None = None
    owner_revision: int
    privacy_epoch: int
    replayed: bool = False
    purge_status: str | None = None


class MemoryRecord(MemoryContract):
    """A versioned SQL fact or independently decidable candidate."""

    memory_id: str
    revision: int
    tenant_id: str
    subject_id: str
    owner_revision: int
    is_current: bool
    kind: str
    status: str
    scope_type: str
    scope_id: str
    field: str | None
    content: str
    content_hash: str
    evidence: tuple[EvidenceRef, ...] = ()
    source_watermark: int
    mutation_id: str
    operation: str
    target_memory_id: str | None = None
    expected_target_revision: int | None = None
    expires_at: datetime | None = None
    forgotten_through_seq: int | None = None
    created_at: datetime


class MemoryOwnerSnapshot(MemoryContract):
    """Version and policy boundary used to freeze per-Turn reads and worker writes."""

    tenant_id: str
    subject_id: str
    memory_revision: int = 0
    source_seq: int = 0
    extraction_revision: int = 0
    consolidated_revision: int = 0
    policy_revision: int = 1
    privacy_epoch: int = 0
    read_enabled: bool = True
    auto_enabled: bool = True
    active_consolidation_event_id: str | None = None
    digest: dict[str, Any] = Field(default_factory=dict)


class MemoryConflict(ValueError):
    """The requested operation no longer matches its immutable concurrency facts."""


class MemoryPermissionError(PermissionError):
    """Identity, source authority or current memory policy denied an operation."""


class MemoryNotFound(LookupError):
    """The requested owner-bound fact is absent or is no longer readable."""


def utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamps without changing PostgreSQL UTC semantics."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
