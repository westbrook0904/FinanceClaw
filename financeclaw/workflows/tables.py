"""SQLAlchemy rows for workflow runs and durable approval decisions."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class WorkflowRunRow(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_workflow_runs_thread"),
        UniqueConstraint("server_run_id", name="uq_workflow_runs_server_run"),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "workflow_version",
            "client_idempotency_key",
            name="uq_workflow_runs_release_idempotency",
        ),
        Index("ix_workflow_runs_owner", "tenant_id", "subject_id", "run_id"),
        Index("ix_workflow_runs_incomplete", "status", "updated_at"),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    assistant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    model_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_timeout_seconds: Mapped[int] = mapped_column(nullable=False)
    approval_timeout_seconds: Mapped[int] = mapped_column(nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_run_id: Mapped[str | None] = mapped_column(String(128))
    client_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    artifact_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowApprovalRow(Base):
    __tablename__ = "workflow_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "approval_point", name="uq_workflow_approval_point"),
        Index("ix_workflow_approvals_owner", "tenant_id", "subject_id", "approval_id"),
        Index("ix_workflow_approvals_pending", "status", "expires_at"),
    )

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_point: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_action: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    allowed_decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    required_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decision_reason: Mapped[str | None] = mapped_column(Text)
