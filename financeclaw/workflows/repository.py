"""Persistence boundary for published workflow runs and approvals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    WorkflowApproval,
    WorkflowApprovalStatus,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from .tables import WorkflowApprovalRow, WorkflowRunRow


class WorkflowNotFound(LookupError):
    pass


class WorkflowConflict(RuntimeError):
    pass


class WorkflowIdempotencyConflict(RuntimeError):
    pass


class WorkflowRepository(Protocol):
    def begin_run(
        self,
        *,
        definition: WorkflowDefinition,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        arguments_hash: str,
        request_fingerprint: str,
        input_payload: dict[str, Any],
    ) -> tuple[WorkflowRun, bool]: ...

    def get_owned(self, run_id: str, tenant_id: str, subject_id: str) -> WorkflowRun: ...

    def bind_server_run(self, run_id: str, server_run_id: str, status: str) -> WorkflowRun: ...

    def set_status(
        self,
        run_id: str,
        status: WorkflowRunStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> tuple[WorkflowRun, bool]: ...

    def list_incomplete(self) -> tuple[WorkflowRun, ...]: ...

    def ensure_approval(self, approval: WorkflowApproval) -> tuple[WorkflowApproval, bool]: ...

    def get_approval(self, run_id: str) -> WorkflowApproval: ...

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: WorkflowApprovalStatus,
        decided_by: str,
        reason: str | None,
        decided_at: datetime,
    ) -> tuple[WorkflowApproval, bool]: ...


def _run(row: WorkflowRunRow) -> WorkflowRun:
    return WorkflowRun(
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        assistant_id=row.assistant_id,
        deployment_revision=row.deployment_revision,
        model_profile_id=row.model_profile_id,
        run_timeout_seconds=row.run_timeout_seconds,
        approval_timeout_seconds=row.approval_timeout_seconds,
        thread_id=row.thread_id,
        server_run_id=row.server_run_id,
        client_idempotency_key=row.client_idempotency_key,
        arguments_hash=row.arguments_hash,
        request_fingerprint=row.request_fingerprint,
        input_payload=row.input_payload,
        output_payload=row.output_payload,
        artifact_refs=tuple(row.artifact_refs),
        status=WorkflowRunStatus(row.status),
        started_at=row.started_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _approval(row: WorkflowApprovalRow) -> WorkflowApproval:
    return WorkflowApproval(
        approval_id=row.approval_id,
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        approval_point=row.approval_point,
        arguments_hash=row.arguments_hash,
        requested_action=row.requested_action,
        request_payload=row.request_payload,
        allowed_decisions=tuple(row.allowed_decisions),
        required_scope=row.required_scope,
        status=WorkflowApprovalStatus(row.status),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
    )


class SqlAlchemyWorkflowRepository:
    """Application-database source of truth; Agent Server owns checkpoints."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def begin_run(
        self,
        *,
        definition: WorkflowDefinition,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        arguments_hash: str,
        request_fingerprint: str,
        input_payload: dict[str, Any],
    ) -> tuple[WorkflowRun, bool]:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == tenant_id,
                    WorkflowRunRow.workflow_id == definition.workflow_id,
                    WorkflowRunRow.workflow_version == definition.version,
                    WorkflowRunRow.client_idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.subject_id != subject_id
                    or existing.request_fingerprint != request_fingerprint
                ):
                    raise WorkflowIdempotencyConflict(
                        "workflow idempotency key was already used for another request"
                    )
                return _run(existing), True
            now = datetime.now(UTC)
            row = WorkflowRunRow(
                run_id=f"run-{uuid4().hex}",
                tenant_id=tenant_id,
                subject_id=subject_id,
                workflow_id=definition.workflow_id,
                workflow_version=definition.version,
                assistant_id=definition.assistant_id,
                deployment_revision=definition.deployment_revision,
                model_profile_id=definition.model_profile_id,
                run_timeout_seconds=definition.timeout_policy.run_timeout_seconds,
                approval_timeout_seconds=definition.timeout_policy.approval_timeout_seconds,
                thread_id=str(uuid4()),
                client_idempotency_key=idempotency_key,
                arguments_hash=arguments_hash,
                request_fingerprint=request_fingerprint,
                input_payload=input_payload,
                artifact_refs=[],
                status=WorkflowRunStatus.ACCEPTED.value,
                started_at=now,
                updated_at=now,
            )
            session.add(row)
        return _run(row), False

    def bind_server_run(self, run_id: str, server_run_id: str, status: str) -> WorkflowRun:
        with self._sessions.begin() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise WorkflowNotFound("workflow run was not found")
            if row.server_run_id is not None and row.server_run_id != server_run_id:
                raise WorkflowConflict("workflow run is already bound to another server run")
            row.server_run_id = server_run_id
            row.status = _normalize_status(status).value
            row.updated_at = datetime.now(UTC)
        return _run(row)

    def get_owned(self, run_id: str, tenant_id: str, subject_id: str) -> WorkflowRun:
        statement = select(WorkflowRunRow).where(
            WorkflowRunRow.run_id == run_id,
            WorkflowRunRow.tenant_id == tenant_id,
            WorkflowRunRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise WorkflowNotFound("workflow run was not found for authenticated owner")
            return _run(row)

    def set_status(
        self,
        run_id: str,
        status: WorkflowRunStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> tuple[WorkflowRun, bool]:
        """Persist a transition and report whether it changed business state."""

        with self._sessions.begin() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise WorkflowNotFound("workflow run was not found")
            terminal = {
                WorkflowRunStatus.COMPLETED.value,
                WorkflowRunStatus.REJECTED.value,
                WorkflowRunStatus.FAILED.value,
            }
            if row.status in terminal and row.status != status.value:
                raise WorkflowConflict("terminal workflow status cannot be changed")
            changed = row.status != status.value
            row.status = status.value
            row.updated_at = datetime.now(UTC)
            if output_payload is not None:
                row.output_payload = output_payload
            if artifact_refs:
                row.artifact_refs = list(dict.fromkeys(artifact_refs))
            if status in {
                WorkflowRunStatus.COMPLETED,
                WorkflowRunStatus.REJECTED,
                WorkflowRunStatus.FAILED,
            }:
                row.completed_at = row.completed_at or datetime.now(UTC)
        return _run(row), changed

    def list_incomplete(self) -> tuple[WorkflowRun, ...]:
        terminal = (
            WorkflowRunStatus.COMPLETED.value,
            WorkflowRunStatus.REJECTED.value,
            WorkflowRunStatus.FAILED.value,
        )
        statement = (
            select(WorkflowRunRow)
            .where(WorkflowRunRow.status.not_in(terminal))
            .order_by(WorkflowRunRow.started_at)
        )
        with self._sessions() as session:
            return tuple(_run(row) for row in session.scalars(statement))

    def ensure_approval(self, approval: WorkflowApproval) -> tuple[WorkflowApproval, bool]:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(WorkflowApprovalRow).where(
                    WorkflowApprovalRow.run_id == approval.run_id,
                    WorkflowApprovalRow.approval_point == approval.approval_point,
                )
            )
            if existing is not None:
                if (
                    existing.approval_id != approval.approval_id
                    or existing.arguments_hash != approval.arguments_hash
                ):
                    raise WorkflowConflict("workflow approval checkpoint changed unexpectedly")
                return _approval(existing), False
            row = WorkflowApprovalRow(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                subject_id=approval.subject_id,
                approval_point=approval.approval_point,
                arguments_hash=approval.arguments_hash,
                requested_action=approval.requested_action,
                request_payload=approval.request_payload,
                allowed_decisions=list(approval.allowed_decisions),
                required_scope=approval.required_scope,
                status=approval.status.value,
                requested_at=approval.requested_at,
                expires_at=approval.expires_at,
            )
            session.add(row)
        return _approval(row), True

    def get_approval(self, run_id: str) -> WorkflowApproval:
        with self._sessions() as session:
            row = session.scalar(
                select(WorkflowApprovalRow)
                .where(WorkflowApprovalRow.run_id == run_id)
                .order_by(WorkflowApprovalRow.requested_at.desc())
            )
            if row is None:
                raise WorkflowNotFound("workflow approval was not found")
            return _approval(row)

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: WorkflowApprovalStatus,
        decided_by: str,
        reason: str | None,
        decided_at: datetime,
    ) -> tuple[WorkflowApproval, bool]:
        with self._sessions.begin() as session:
            row = session.get(WorkflowApprovalRow, approval_id)
            if row is None:
                raise WorkflowNotFound("workflow approval was not found")
            if row.status != WorkflowApprovalStatus.PENDING.value:
                if row.status == status.value and row.decided_by == decided_by:
                    return _approval(row), False
                raise WorkflowConflict("workflow approval has already been decided")
            row.status = status.value
            row.decided_by = decided_by
            row.decision_reason = reason
            row.decided_at = decided_at
        return _approval(row), True


def _normalize_status(status: str) -> WorkflowRunStatus:
    return {
        "accepted": WorkflowRunStatus.ACCEPTED,
        "pending": WorkflowRunStatus.PENDING,
        "running": WorkflowRunStatus.RUNNING,
        "interrupted": WorkflowRunStatus.INTERRUPTED,
        "success": WorkflowRunStatus.COMPLETED,
        "completed": WorkflowRunStatus.COMPLETED,
        "error": WorkflowRunStatus.FAILED,
        "failed": WorkflowRunStatus.FAILED,
    }.get(status, WorkflowRunStatus.PENDING)
