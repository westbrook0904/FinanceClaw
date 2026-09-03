"""Typed handoff and durable parent-child delegation records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ContextReference = Annotated[str, Field(min_length=1, max_length=256)]


class FrozenDelegationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DelegationKind(StrEnum):
    WORKFLOW = "workflow"
    AGENT = "agent"


class DelegationStatus(StrEnum):
    REQUESTED = "requested"
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    DELIVERED = "delivered"


class WorkflowHandoff(FrozenDelegationModel):
    """Unversioned workflow intent emitted by the trusted delegation Tool."""

    schema_version: Literal[1] = 1
    handoff_id: str = Field(min_length=1, max_length=128)
    kind: Literal[DelegationKind.WORKFLOW] = DelegationKind.WORKFLOW
    parent_run_id: str = Field(min_length=1, max_length=128)
    parent_turn_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]


class AgentHandoff(FrozenDelegationModel):
    """Bounded domain-Agent task emitted by the trusted delegation Tool."""

    schema_version: Literal[1] = 1
    handoff_id: str = Field(min_length=1, max_length=128)
    kind: Literal[DelegationKind.AGENT] = DelegationKind.AGENT
    parent_run_id: str = Field(min_length=1, max_length=128)
    parent_turn_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=8_000)
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=32)


HandoffRequest = Annotated[WorkflowHandoff | AgentHandoff, Field(discriminator="kind")]
HANDOFF_ADAPTER = TypeAdapter(HandoffRequest)


class DelegationResult(FrozenDelegationModel):
    """Bounded child result supplied when the parent Tool is resumed."""

    schema_version: Literal[1] = 1
    delegation_id: str
    kind: DelegationKind
    target_id: str
    target_version: str
    child_run_id: str
    status: Literal["completed", "rejected", "failed"]
    output: dict[str, Any] | None = None
    error: str | None = None


class DelegationRecord(FrozenDelegationModel):
    delegation_id: str
    tenant_id: str
    subject_id: str
    conversation_id: str
    parent_turn_id: str
    parent_run_id: str
    kind: DelegationKind
    target_id: str
    target_version: str
    arguments: dict[str, Any]
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_decision: Literal["allowed"] = "allowed"
    policy_version: str = "delegation-policy/1.0.0"
    child_run_id: str | None = None
    child_thread_id: str | None = None
    child_server_run_id: str | None = None
    status: DelegationStatus
    output_payload: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    delivered_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.DELIVERED,
        }


class AgentDelegationInput(BaseModel):
    """Model-visible input for one domain-Agent delegation capability."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=8_000)
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=32)
