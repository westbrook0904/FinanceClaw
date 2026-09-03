"""Bounded, secret-free financial audit records."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AuditEventType(StrEnum):
    TOOL_ALLOWED = "tool.allowed"
    TOOL_DENIED = "tool.denied"
    TOOL_APPROVAL_REQUESTED = "tool.approval_requested"
    TOOL_APPROVED = "tool.approved"
    TOOL_REJECTED = "tool.rejected"
    FINANCIAL_TOOL_EXECUTED = "financial_tool.executed"
    FINANCIAL_TOOL_FAILED = "financial_tool.failed"
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_COMMITTED = "memory.committed"
    MEMORY_SUPERSEDED = "memory.superseded"
    MEMORY_REVOKED = "memory.revoked"
    MEMORY_DELETED = "memory.deleted"
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_INTERRUPTED = "workflow.interrupted"
    WORKFLOW_APPROVED = "workflow.approved"
    WORKFLOW_REJECTED = "workflow.rejected"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    DELEGATION_REQUESTED = "delegation.requested"
    DELEGATION_STARTED = "delegation.started"
    DELEGATION_INTERRUPTED = "delegation.interrupted"
    DELEGATION_COMPLETED = "delegation.completed"
    DELEGATION_FAILED = "delegation.failed"
    DELEGATION_DELIVERED = "delegation.delivered"


class AuditRecord(BaseModel):
    """Stable audit fact; arguments and provider payloads are represented by hashes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str = Field(default_factory=lambda: f"audit-{uuid4().hex}")
    event_type: AuditEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: str
    subject_id: str
    conversation_id: str | None = None
    turn_id: str
    run_id: str
    tool_call_id: str | None = None
    resource_type: str = "tool"
    resource_id: str
    resource_version: str
    action: str
    decision: str
    policy_version: str
    payload_hash: str
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
