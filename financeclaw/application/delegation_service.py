"""Durable orchestration between a suspended parent Agent and child runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from financeclaw.agents import AgentProfileCatalog
from financeclaw.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.contracts import (
    ApprovalDecision,
    ExecutionContext,
    RunStatusResponse,
    WorkflowTarget,
)
from financeclaw.delegation import (
    HANDOFF_ADAPTER,
    AgentDelegationInput,
    DelegationConflict,
    DelegationKind,
    DelegationNotFound,
    DelegationRecord,
    DelegationRepository,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    WorkflowHandoff,
)
from financeclaw.workflows import WorkflowRunStatus

from .agent_server_client import AgentServerClient
from .run_service import RunNotFound
from .workflow_service import WorkflowAuthorizationError, WorkflowInputError, WorkflowService


class DelegationInputError(ValueError):
    pass


class DelegationAuthorizationError(PermissionError):
    pass


class DelegationService:
    """Create, monitor and complete child runs without owning their runtimes."""

    def __init__(
        self,
        client: AgentServerClient,
        repository: DelegationRepository,
        workflow_service: WorkflowService,
        agent_profiles: AgentProfileCatalog,
        audit: AuditRepository,
    ) -> None:
        self.client = client
        self.repository = repository
        self.workflow_service = workflow_service
        self.agent_profiles = agent_profiles
        self.audit = audit

    async def start(
        self,
        handoff: HandoffRequest,
        *,
        parent_run_id: str,
        parent_turn_id: str,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
    ) -> DelegationRecord:
        self._verify_parent(
            handoff,
            parent_run_id=parent_run_id,
            parent_turn_id=parent_turn_id,
            conversation_id=conversation_id,
        )
        kind, target_id, target_version, arguments = self._resolve(handoff, scopes)
        record, created = await asyncio.to_thread(
            self.repository.ensure_requested,
            delegation_id=handoff.handoff_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            conversation_id=conversation_id,
            parent_turn_id=parent_turn_id,
            parent_run_id=parent_run_id,
            kind=kind,
            target_id=target_id,
            target_version=target_version,
            arguments=arguments,
        )
        if created:
            await self._audit(
                record,
                AuditEventType.DELEGATION_REQUESTED,
                decision="requested",
            )
        if record.child_run_id is None:
            if record.kind is DelegationKind.WORKFLOW:
                record = await self._start_workflow(record, scopes)
            else:
                record = await self._start_agent(record, scopes)
        return record

    async def status(
        self, delegation_id: str, *, tenant_id: str, subject_id: str
    ) -> DelegationRecord:
        record = await asyncio.to_thread(
            self.repository.get_owned, delegation_id, tenant_id, subject_id
        )
        if record.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.DELIVERED,
        }:
            return record
        if record.child_run_id is None:
            # The authorization decision and normalized arguments were committed
            # before child creation. Replays recover from a crash in that gap by
            # reusing the handoff ID as the child's idempotency key.
            recovery_scopes = frozenset({"*"})
            if record.kind is DelegationKind.WORKFLOW:
                record = await self._start_workflow(record, recovery_scopes)
            else:
                record = await self._start_agent(record, recovery_scopes)
        elif record.kind is DelegationKind.AGENT and record.child_server_run_id is None:
            # Agent child identity is stored before its remote create call. A
            # metadata lookup makes the remaining retry safe after BFF restart.
            record = await self._start_agent(record, frozenset({"*"}))
        if record.kind is DelegationKind.WORKFLOW:
            child = await self.workflow_service.status(
                record.child_run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            return await self._sync_child_status(record, child)
        return await self._agent_status(record)

    async def resume(
        self,
        record: DelegationRecord,
        decision: ApprovalDecision,
        *,
        scopes: frozenset[str],
    ) -> DelegationRecord:
        if record.kind is not DelegationKind.WORKFLOW or record.child_run_id is None:
            raise DelegationConflict("delegated child does not support approval resume")
        child = await self.workflow_service.resume(
            record.child_run_id,
            decision,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
        )
        return await self._sync_child_status(record, child)

    async def child_status(
        self, child_run_id: str, *, tenant_id: str, subject_id: str
    ) -> RunStatusResponse:
        try:
            record = await asyncio.to_thread(
                self.repository.get_by_child_owned,
                child_run_id,
                tenant_id,
                subject_id,
            )
        except DelegationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        current = await self.status(
            record.delegation_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        if current.child_run_id is None or current.child_thread_id is None:
            raise RunNotFound("delegated child run has not started")
        return RunStatusResponse(
            run_id=current.child_run_id,
            thread_id=current.child_thread_id,
            status=current.status.value,
            output=current.output_payload,
        )

    async def mark_delivered(self, record: DelegationRecord) -> DelegationRecord:
        delivered, changed = await asyncio.to_thread(
            self.repository.set_status,
            record.delegation_id,
            DelegationStatus.DELIVERED,
        )
        if changed:
            await self._audit(
                delivered,
                AuditEventType.DELEGATION_DELIVERED,
                decision="delivered_to_parent",
            )
        return delivered

    async def latest_for_parent(
        self, parent_run_id: str, *, tenant_id: str, subject_id: str
    ) -> DelegationRecord | None:
        return await asyncio.to_thread(
            self.repository.latest_undelivered_for_parent,
            parent_run_id,
            tenant_id,
            subject_id,
        )

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        records = await asyncio.to_thread(self.repository.list_undelivered)
        reconciled: list[str] = []
        for record in records:
            await self.status(
                record.delegation_id,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
            )
            reconciled.append(record.delegation_id)
        return tuple(reconciled)

    @staticmethod
    def result(record: DelegationRecord) -> DelegationResult:
        if record.child_run_id is None or record.status not in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
        }:
            raise DelegationConflict("delegation has no terminal child result")
        return DelegationResult(
            delegation_id=record.delegation_id,
            kind=record.kind,
            target_id=record.target_id,
            target_version=record.target_version,
            child_run_id=record.child_run_id,
            status=record.status.value,
            output=record.output_payload,
            error=record.error,
        )

    async def _start_workflow(
        self, record: DelegationRecord, scopes: frozenset[str]
    ) -> DelegationRecord:
        try:
            accepted = await self.workflow_service.start(
                WorkflowTarget(
                    workflow_id=record.target_id,
                    version=record.target_version,
                    arguments=record.arguments,
                ),
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
                scopes=scopes,
                idempotency_key=record.delegation_id,
            )
        except (WorkflowAuthorizationError, WorkflowInputError) as exc:
            await self._fail_start(record, str(exc))
            raise
        workflow = await asyncio.to_thread(
            self.workflow_service.repository.get_owned,
            accepted.run_id,
            record.tenant_id,
            record.subject_id,
        )
        bound = await asyncio.to_thread(
            self.repository.bind_child,
            record.delegation_id,
            child_run_id=workflow.run_id,
            child_thread_id=workflow.thread_id,
            child_server_run_id=workflow.server_run_id,
            status=_delegation_status(workflow.status.value),
        )
        await self._audit(bound, AuditEventType.DELEGATION_STARTED, decision="child_started")
        return bound

    async def _start_agent(
        self, record: DelegationRecord, scopes: frozenset[str]
    ) -> DelegationRecord:
        profile = self.agent_profiles.resolve(record.target_id, record.target_version)
        self._require_scopes(scopes, profile.required_scopes)
        prepared = await asyncio.to_thread(
            self.repository.prepare_agent_child, record.delegation_id
        )
        if prepared.child_run_id is None or prepared.child_thread_id is None:
            raise DelegationConflict("Agent child identity was not prepared")
        await self.client.create_thread(prepared.child_thread_id)
        server_run = await self.client.find_run(
            thread_id=prepared.child_thread_id,
            application_run_id=prepared.child_run_id,
        )
        if server_run is None:
            context = ExecutionContext(
                tenant_id=prepared.tenant_id,
                subject_id=prepared.subject_id,
                scopes=scopes,
                conversation_id=prepared.conversation_id,
                turn_id=prepared.parent_turn_id,
                run_id=prepared.child_run_id,
            )
            server_run = await self.client.create_run(
                thread_id=prepared.child_thread_id,
                assistant_id=profile.agent_id,
                input={"messages": [{"role": "user", "content": prepared.arguments["task"]}]},
                context=context.model_dump(mode="json"),
                metadata={
                    **context.trace_metadata(),
                    "application_run_id": prepared.child_run_id,
                    "target_kind": "agent_delegation",
                    "agent_id": profile.agent_id,
                    "agent_profile_version": profile.version,
                    "parent_run_id": prepared.parent_run_id,
                    "delegation_id": prepared.delegation_id,
                },
            )
        bound = await asyncio.to_thread(
            self.repository.bind_child,
            prepared.delegation_id,
            child_run_id=prepared.child_run_id,
            child_thread_id=prepared.child_thread_id,
            child_server_run_id=server_run.run_id,
            status=_delegation_status(server_run.status),
        )
        await self._audit(bound, AuditEventType.DELEGATION_STARTED, decision="child_started")
        return bound

    async def _agent_status(self, record: DelegationRecord) -> DelegationRecord:
        if record.child_thread_id is None or record.child_server_run_id is None:
            return record
        server = await self.client.get_run(
            thread_id=record.child_thread_id,
            run_id=record.child_server_run_id,
        )
        status = str(server.get("status", record.status.value))
        if status in {"success", "completed"}:
            raw = await self.client.join_run(
                thread_id=record.child_thread_id,
                run_id=record.child_server_run_id,
            )
            output = {"message": _final_assistant_content(raw) or ""}
            return await self._transition(
                record,
                DelegationStatus.COMPLETED,
                output=output,
            )
        if status in {"error", "failed"}:
            return await self._transition(
                record,
                DelegationStatus.FAILED,
                error="domain Agent child run failed",
            )
        if status == "interrupted":
            return await self._transition(record, DelegationStatus.INTERRUPTED)
        return await self._transition(record, _delegation_status(status))

    async def _sync_child_status(
        self, record: DelegationRecord, child: RunStatusResponse
    ) -> DelegationRecord:
        status = _delegation_status(child.status)
        error = "delegated Workflow failed" if status is DelegationStatus.FAILED else None
        return await self._transition(record, status, output=child.output, error=error)

    async def _transition(
        self,
        record: DelegationRecord,
        status: DelegationStatus,
        *,
        output: dict[str, Any] | list[Any] | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        normalized_output = output if isinstance(output, dict) else None
        updated, changed = await asyncio.to_thread(
            self.repository.set_status,
            record.delegation_id,
            status,
            output_payload=normalized_output,
            error=error,
        )
        if changed:
            event = {
                DelegationStatus.INTERRUPTED: AuditEventType.DELEGATION_INTERRUPTED,
                DelegationStatus.COMPLETED: AuditEventType.DELEGATION_COMPLETED,
                DelegationStatus.REJECTED: AuditEventType.DELEGATION_COMPLETED,
                DelegationStatus.FAILED: AuditEventType.DELEGATION_FAILED,
            }.get(status)
            if event is not None:
                await self._audit(updated, event, decision=status.value)
        return updated

    async def _fail_start(self, record: DelegationRecord, error: str) -> None:
        await self._transition(record, DelegationStatus.FAILED, error=error)

    def _resolve(
        self, handoff: HandoffRequest, scopes: frozenset[str]
    ) -> tuple[DelegationKind, str, str, dict[str, Any]]:
        try:
            if isinstance(handoff, WorkflowHandoff):
                definition = self.workflow_service.catalog.resolve(handoff.workflow_id)
                self._require_scopes(scopes, definition.required_scopes)
                return (
                    DelegationKind.WORKFLOW,
                    definition.workflow_id,
                    definition.version,
                    definition.normalize_input(handoff.arguments),
                )
            profile = self.agent_profiles.resolve(handoff.agent_id)
            if not profile.delegatable:
                raise DelegationInputError("AgentProfile is not available for delegation")
            self._require_scopes(scopes, profile.required_scopes)
            arguments = AgentDelegationInput(
                task=handoff.task,
                context_refs=handoff.context_refs,
            ).model_dump(mode="json")
            return DelegationKind.AGENT, profile.agent_id, profile.version, arguments
        except DelegationAuthorizationError:
            raise
        except Exception as exc:
            raise DelegationInputError(str(exc)) from exc

    @staticmethod
    def _verify_parent(
        handoff: HandoffRequest,
        *,
        parent_run_id: str,
        parent_turn_id: str,
        conversation_id: str,
    ) -> None:
        if (
            handoff.parent_run_id != parent_run_id
            or handoff.parent_turn_id != parent_turn_id
            or handoff.conversation_id != conversation_id
        ):
            raise DelegationInputError("handoff parent references do not match the owned turn")

    @staticmethod
    def _require_scopes(granted: frozenset[str], required: frozenset[str]) -> None:
        if "*" not in granted and not required.issubset(granted):
            raise DelegationAuthorizationError("required delegation scope is missing")

    async def _audit(
        self, record: DelegationRecord, event: AuditEventType, *, decision: str
    ) -> None:
        await asyncio.to_thread(
            self.audit.append,
            AuditRecord(
                event_type=event,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
                conversation_id=record.conversation_id,
                turn_id=record.parent_turn_id,
                run_id=record.parent_run_id,
                resource_type="delegation",
                resource_id=record.target_id,
                resource_version=record.target_version,
                action=record.kind.value,
                decision=decision,
                policy_version="delegation-policy/1.0.0",
                payload_hash=record.arguments_hash,
                evidence_refs=(record.delegation_id,),
                metadata={"child_run_id": record.child_run_id},
            ),
        )


def extract_handoff_interrupt(value: Mapping[str, Any]) -> HandoffRequest | None:
    """Return a typed handoff from Agent Server interrupt shapes, if present."""

    raw_items = value.get("interrupts") or value.get("__interrupt__") or ()
    if isinstance(raw_items, Mapping):
        raw_items = (raw_items,)
    for item in raw_items if isinstance(raw_items, (list, tuple)) else ():
        raw = getattr(item, "value", None)
        if raw is None and isinstance(item, Mapping):
            raw = item.get("value", item)
        if not isinstance(raw, Mapping):
            continue
        if raw.get("schema_version") != 1 or "handoff_id" not in raw:
            continue
        try:
            return HANDOFF_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise DelegationInputError("Agent returned an invalid typed handoff") from exc
    return None


def delegation_projection(record: DelegationRecord) -> dict[str, Any]:
    return {
        "delegation_id": record.delegation_id,
        "kind": record.kind.value,
        "target_id": record.target_id,
        "target_version": record.target_version,
        "child_run_id": record.child_run_id,
        "status": record.status.value,
        "output": record.output_payload,
        "error": record.error,
    }


def _delegation_status(status: str) -> DelegationStatus:
    return {
        WorkflowRunStatus.ACCEPTED.value: DelegationStatus.PENDING,
        WorkflowRunStatus.PENDING.value: DelegationStatus.PENDING,
        WorkflowRunStatus.RUNNING.value: DelegationStatus.RUNNING,
        WorkflowRunStatus.INTERRUPTED.value: DelegationStatus.INTERRUPTED,
        WorkflowRunStatus.COMPLETED.value: DelegationStatus.COMPLETED,
        WorkflowRunStatus.REJECTED.value: DelegationStatus.REJECTED,
        WorkflowRunStatus.FAILED.value: DelegationStatus.FAILED,
        "success": DelegationStatus.COMPLETED,
    }.get(status, DelegationStatus.PENDING)


def _final_assistant_content(output: Mapping[str, Any]) -> str | None:
    messages = output.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return (
                message.content
                if isinstance(message.content, str)
                else json.dumps(message.content, default=str)
            )
        if isinstance(message, Mapping) and message.get("type") in {"ai", "assistant"}:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content, default=str)
    return None
