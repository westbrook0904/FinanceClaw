"""Persistence boundary for typed handoffs and their child runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import DelegationKind, DelegationRecord, DelegationStatus
from .tables import DelegationRow


class DelegationNotFound(LookupError):
    pass


class DelegationConflict(RuntimeError):
    pass


class DelegationRepository(Protocol):
    def ensure_requested(
        self,
        *,
        delegation_id: str,
        tenant_id: str,
        subject_id: str,
        conversation_id: str,
        parent_turn_id: str,
        parent_run_id: str,
        kind: DelegationKind,
        target_id: str,
        target_version: str,
        arguments: dict[str, Any],
    ) -> tuple[DelegationRecord, bool]: ...

    def prepare_agent_child(self, delegation_id: str) -> DelegationRecord: ...

    def bind_child(
        self,
        delegation_id: str,
        *,
        child_run_id: str,
        child_thread_id: str,
        child_server_run_id: str | None,
        status: DelegationStatus,
    ) -> DelegationRecord: ...

    def set_status(
        self,
        delegation_id: str,
        status: DelegationStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> tuple[DelegationRecord, bool]: ...

    def get_owned(
        self, delegation_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord: ...

    def get_by_child_owned(
        self, child_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord: ...

    def latest_undelivered_for_parent(
        self, parent_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord | None: ...

    def list_undelivered(self) -> tuple[DelegationRecord, ...]: ...


def _record(row: DelegationRow) -> DelegationRecord:
    return DelegationRecord(
        delegation_id=row.delegation_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        conversation_id=row.conversation_id,
        parent_turn_id=row.parent_turn_id,
        parent_run_id=row.parent_run_id,
        kind=DelegationKind(row.kind),
        target_id=row.target_id,
        target_version=row.target_version,
        arguments=row.arguments,
        arguments_hash=row.arguments_hash,
        request_fingerprint=row.request_fingerprint,
        authorization_decision=row.authorization_decision,
        policy_version=row.policy_version,
        child_run_id=row.child_run_id,
        child_thread_id=row.child_thread_id,
        child_server_run_id=row.child_server_run_id,
        status=DelegationStatus(row.status),
        output_payload=row.output_payload,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        delivered_at=row.delivered_at,
    )


class SqlAlchemyDelegationRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def ensure_requested(
        self,
        *,
        delegation_id: str,
        tenant_id: str,
        subject_id: str,
        conversation_id: str,
        parent_turn_id: str,
        parent_run_id: str,
        kind: DelegationKind,
        target_id: str,
        target_version: str,
        arguments: dict[str, Any],
    ) -> tuple[DelegationRecord, bool]:
        arguments_hash = _hash(arguments)
        fingerprint = _hash(
            {
                "conversation_id": conversation_id,
                "parent_turn_id": parent_turn_id,
                "parent_run_id": parent_run_id,
                "kind": kind.value,
                "target_id": target_id,
                "target_version": target_version,
                "arguments": arguments,
            }
        )
        with self._sessions.begin() as session:
            existing = session.get(DelegationRow, delegation_id)
            if existing is not None:
                if (
                    existing.tenant_id != tenant_id
                    or existing.subject_id != subject_id
                    or existing.request_fingerprint != fingerprint
                ):
                    raise DelegationConflict("handoff ID was reused for another delegation")
                return _record(existing), False
            now = datetime.now(UTC)
            row = DelegationRow(
                delegation_id=delegation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                conversation_id=conversation_id,
                parent_turn_id=parent_turn_id,
                parent_run_id=parent_run_id,
                kind=kind.value,
                target_id=target_id,
                target_version=target_version,
                arguments=arguments,
                arguments_hash=arguments_hash,
                request_fingerprint=fingerprint,
                authorization_decision="allowed",
                policy_version="delegation-policy/1.0.0",
                status=DelegationStatus.REQUESTED.value,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        return _record(row), True

    def prepare_agent_child(self, delegation_id: str) -> DelegationRecord:
        with self._sessions.begin() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise DelegationNotFound("delegation was not found")
            if row.kind != DelegationKind.AGENT.value:
                raise DelegationConflict("only Agent delegation prepares its own child run")
            if row.child_run_id is None:
                row.child_run_id = f"run-{uuid4().hex}"
                row.child_thread_id = str(uuid4())
                row.status = DelegationStatus.PENDING.value
                row.updated_at = datetime.now(UTC)
        return _record(row)

    def bind_child(
        self,
        delegation_id: str,
        *,
        child_run_id: str,
        child_thread_id: str,
        child_server_run_id: str | None,
        status: DelegationStatus,
    ) -> DelegationRecord:
        with self._sessions.begin() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise DelegationNotFound("delegation was not found")
            if row.child_run_id is not None and row.child_run_id != child_run_id:
                raise DelegationConflict("delegation is already bound to another child run")
            if row.child_thread_id is not None and row.child_thread_id != child_thread_id:
                raise DelegationConflict("delegation is already bound to another child thread")
            if (
                row.child_server_run_id is not None
                and child_server_run_id is not None
                and row.child_server_run_id != child_server_run_id
            ):
                raise DelegationConflict("delegation is already bound to another server run")
            row.child_run_id = child_run_id
            row.child_thread_id = child_thread_id
            if child_server_run_id is not None:
                row.child_server_run_id = child_server_run_id
            row.status = status.value
            row.updated_at = datetime.now(UTC)
        return _record(row)

    def set_status(
        self,
        delegation_id: str,
        status: DelegationStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> tuple[DelegationRecord, bool]:
        with self._sessions.begin() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise DelegationNotFound("delegation was not found")
            current = DelegationStatus(row.status)
            terminal = {
                DelegationStatus.COMPLETED,
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
                DelegationStatus.DELIVERED,
            }
            if current in terminal and not (
                current is not DelegationStatus.DELIVERED and status is DelegationStatus.DELIVERED
            ):
                if current is status:
                    return _record(row), False
                raise DelegationConflict("terminal delegation status cannot be changed")
            changed = current is not status
            now = datetime.now(UTC)
            row.status = status.value
            row.updated_at = now
            if output_payload is not None:
                row.output_payload = output_payload
            if error is not None:
                row.error = error
            if status in {
                DelegationStatus.COMPLETED,
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
            }:
                row.completed_at = row.completed_at or now
            if status is DelegationStatus.DELIVERED:
                row.delivered_at = row.delivered_at or now
        return _record(row), changed

    def get_owned(self, delegation_id: str, tenant_id: str, subject_id: str) -> DelegationRecord:
        statement = select(DelegationRow).where(
            DelegationRow.delegation_id == delegation_id,
            DelegationRow.tenant_id == tenant_id,
            DelegationRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise DelegationNotFound("delegation was not found for authenticated owner")
            return _record(row)

    def get_by_child_owned(
        self, child_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord:
        statement = select(DelegationRow).where(
            DelegationRow.child_run_id == child_run_id,
            DelegationRow.tenant_id == tenant_id,
            DelegationRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise DelegationNotFound(
                    "delegated child run was not found for authenticated owner"
                )
            return _record(row)

    def latest_undelivered_for_parent(
        self, parent_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord | None:
        statement = (
            select(DelegationRow)
            .where(
                DelegationRow.parent_run_id == parent_run_id,
                DelegationRow.tenant_id == tenant_id,
                DelegationRow.subject_id == subject_id,
                DelegationRow.status != DelegationStatus.DELIVERED.value,
            )
            .order_by(DelegationRow.created_at.desc())
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            return _record(row) if row is not None else None

    def list_undelivered(self) -> tuple[DelegationRecord, ...]:
        statement = (
            select(DelegationRow)
            .where(DelegationRow.status != DelegationStatus.DELIVERED.value)
            .order_by(DelegationRow.created_at)
        )
        with self._sessions() as session:
            return tuple(_record(row) for row in session.scalars(statement))


def _hash(value: dict[str, Any]) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
