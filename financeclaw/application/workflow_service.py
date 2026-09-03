"""Persistent BFF orchestration for code-published workflow runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from financeclaw.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ExecutionContext,
    RunAccepted,
    RunStatusResponse,
    StreamEvent,
    WorkflowTarget,
)
from financeclaw.workflows import (
    WorkflowApproval,
    WorkflowApprovalStatus,
    WorkflowCatalog,
    WorkflowConflict,
    WorkflowIdempotencyConflict,
    WorkflowNotFound,
    WorkflowRepository,
    WorkflowRun,
    WorkflowRunStatus,
)

from .agent_server_client import AgentServerClient
from .run_service import IdempotencyConflict, RunNotFound


class WorkflowAuthorizationError(PermissionError):
    pass


class WorkflowApprovalExpired(RuntimeError):
    pass


class WorkflowInputError(ValueError):
    pass


class WorkflowService:
    """Map durable business runs to Agent Server threads without a second runtime."""

    def __init__(
        self,
        client: AgentServerClient,
        repository: WorkflowRepository,
        catalog: WorkflowCatalog,
        audit: AuditRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.catalog = catalog
        self.audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))

    async def start(
        self,
        target: WorkflowTarget,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
    ) -> RunAccepted:
        try:
            definition = self.catalog.resolve(target.workflow_id, target.version)
            normalized = definition.normalize_input(target.arguments)
        except Exception as exc:
            raise WorkflowInputError(str(exc)) from exc
        self._require_scopes(scopes, definition.required_scopes)
        arguments_hash = _hash(normalized)
        request_fingerprint = _hash(
            {
                "workflow_id": definition.workflow_id,
                "workflow_version": definition.version,
                "arguments": normalized,
            }
        )
        try:
            record, replay = await asyncio.to_thread(
                self.repository.begin_run,
                definition=definition,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_key=idempotency_key,
                arguments_hash=arguments_hash,
                request_fingerprint=request_fingerprint,
                input_payload=normalized,
            )
        except WorkflowIdempotencyConflict as exc:
            raise IdempotencyConflict(str(exc)) from exc
        if not replay:
            await self._audit(
                record,
                AuditEventType.WORKFLOW_STARTED,
                decision="started",
                payload_hash=arguments_hash,
            )
        if record.server_run_id is None:
            await self.client.create_thread(record.thread_id)
            context = self._context(record, scopes)
            server_run = (
                await self.client.find_run(
                    thread_id=record.thread_id,
                    application_run_id=record.run_id,
                )
                if replay
                else None
            )
            if server_run is None:
                server_run = await self.client.create_run(
                    thread_id=record.thread_id,
                    assistant_id=record.assistant_id,
                    input=record.input_payload,
                    context=context.model_dump(mode="json"),
                    metadata=self._metadata(record, context),
                )
            record = await asyncio.to_thread(
                self.repository.bind_server_run,
                record.run_id,
                server_run.run_id,
                server_run.status,
            )
        return RunAccepted(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status.value,
            target_kind="workflow",
            idempotent_replay=replay,
        )

    async def status(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        record = await self._owned(run_id, tenant_id, subject_id)
        if record.status in _TERMINAL:
            return self._response(record)
        if record.server_run_id is None:
            return self._response(record)
        if record.status is not WorkflowRunStatus.INTERRUPTED and self._now() >= _aware(
            record.started_at
        ) + timedelta(seconds=record.run_timeout_seconds):
            failed, changed = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.FAILED
            )
            if changed:
                await self._audit(
                    failed,
                    AuditEventType.WORKFLOW_FAILED,
                    decision="run_timeout",
                    payload_hash=failed.arguments_hash,
                )
            return self._response(failed)

        server = await self.client.get_run(
            thread_id=record.thread_id,
            run_id=record.server_run_id,
        )
        server_status = str(server.get("status", record.status.value))
        if server_status == "interrupted":
            return await self._record_interrupt(record, server)
        if server_status in {"error", "failed"}:
            failed, changed = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.FAILED
            )
            if changed:
                await self._audit(
                    failed,
                    AuditEventType.WORKFLOW_FAILED,
                    decision="server_failed",
                    payload_hash=failed.arguments_hash,
                )
            return self._response(failed)
        if server_status in {"success", "completed"}:
            output = await self.client.join_run(
                thread_id=record.thread_id,
                run_id=record.server_run_id,
            )
            return await self._complete(record, output)
        pending, _ = await asyncio.to_thread(
            self.repository.set_status,
            record.run_id,
            _server_status(server_status),
        )
        return self._response(pending)

    async def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
    ) -> RunStatusResponse:
        record = await self._owned(run_id, tenant_id, subject_id)
        if record.status in _TERMINAL:
            return self._response(record)
        if record.status is not WorkflowRunStatus.INTERRUPTED or record.server_run_id is None:
            raise WorkflowConflict("workflow run is not waiting for approval")
        approval = await asyncio.to_thread(self.repository.get_approval, record.run_id)
        self._require_scopes(scopes, frozenset({approval.required_scope}))
        current = self._now()
        if current >= _aware(approval.expires_at):
            await asyncio.to_thread(
                self.repository.decide_approval,
                approval.approval_id,
                status=WorkflowApprovalStatus.EXPIRED,
                decided_by=subject_id,
                reason="approval timeout",
                decided_at=current,
            )
            failed, changed = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.FAILED
            )
            if changed:
                await self._audit(
                    failed,
                    AuditEventType.WORKFLOW_FAILED,
                    decision="approval_timeout",
                    payload_hash=approval.arguments_hash,
                    resource_id=approval.approval_id,
                    resource_type="workflow_approval",
                )
            raise WorkflowApprovalExpired("workflow approval window has expired")
        if decision.type is ApprovalDecisionType.EDIT:
            raise WorkflowInputError("published workflow approval does not allow input edits")
        if decision.type.value not in approval.allowed_decisions:
            raise WorkflowInputError("unsupported workflow approval decision")
        if decision.arguments_hash != approval.arguments_hash:
            raise WorkflowConflict("approval hash does not match the published workflow input")

        approval_status = (
            WorkflowApprovalStatus.APPROVED
            if decision.type is ApprovalDecisionType.APPROVE
            else WorkflowApprovalStatus.REJECTED
        )
        decided, changed = await asyncio.to_thread(
            self.repository.decide_approval,
            approval.approval_id,
            status=approval_status,
            decided_by=subject_id,
            reason=decision.reason,
            decided_at=current,
        )
        if changed:
            await self._audit(
                record,
                (
                    AuditEventType.WORKFLOW_APPROVED
                    if approval_status is WorkflowApprovalStatus.APPROVED
                    else AuditEventType.WORKFLOW_REJECTED
                ),
                decision=approval_status.value,
                payload_hash=approval.arguments_hash,
                resource_id=decided.approval_id,
                resource_type="workflow_approval",
            )
        context = self._context(record, scopes)
        result = await self.client.resume_run(
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
            command={
                "resume": {
                    "decisions": [
                        {
                            "type": decision.type.value,
                            "arguments_hash": decision.arguments_hash,
                            **({"message": decision.reason} if decision.reason else {}),
                        }
                    ]
                }
            },
            context=context.model_dump(mode="json"),
            metadata=self._metadata(record, context),
        )
        if result.get("__interrupt__"):
            return await self._record_interrupt(record, result)
        return await self._complete(record, result)

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        records = await asyncio.to_thread(self.repository.list_incomplete)
        reconciled: list[str] = []
        for record in records:
            if record.server_run_id is None:
                continue
            await self.status(
                record.run_id,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
            )
            reconciled.append(record.run_id)
        return tuple(reconciled)

    async def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        await self._owned(run_id, tenant_id, subject_id)

    async def stream(
        self, run_id: str, *, tenant_id: str, subject_id: str
    ) -> AsyncIterator[StreamEvent]:
        record = await self._owned(run_id, tenant_id, subject_id)
        async for part in self.client.stream_thread(
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
        ):
            if isinstance(part, Mapping):
                yield StreamEvent(
                    event=str(part.get("event", "message")),
                    data=part.get("data", dict(part)),
                )
            else:
                yield StreamEvent(
                    event=str(getattr(part, "event", "message")),
                    data=getattr(part, "data", repr(part)),
                )

    async def _record_interrupt(
        self, record: WorkflowRun, server: Mapping[str, Any]
    ) -> RunStatusResponse:
        payload = _interrupt_payload(server)
        if (
            payload.get("workflow_id") != record.workflow_id
            or payload.get("workflow_version") != record.workflow_version
            or payload.get("arguments_hash") != record.arguments_hash
        ):
            raise WorkflowConflict("Agent Server returned a mismatched workflow approval")
        try:
            definition = self.catalog[(record.workflow_id, record.workflow_version)]
            approval_point = next(
                point
                for point in definition.approval_points
                if point.approval_id == payload["approval_point"]
            )
        except (KeyError, StopIteration) as exc:
            raise WorkflowConflict("workflow returned an unpublished approval point") from exc
        if (
            tuple(payload["allowed_decisions"]) != approval_point.allowed_decisions
            or payload["required_scope"] != approval_point.required_scope
            or payload["requested_action"] != approval_point.requested_action
        ):
            raise WorkflowConflict("workflow approval policy differs from its published release")
        requested_at = self._now()
        approval = WorkflowApproval(
            approval_id=str(payload["approval_id"]),
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            approval_point=str(payload["approval_point"]),
            arguments_hash=record.arguments_hash,
            requested_action=approval_point.requested_action,
            request_payload=dict(payload),
            allowed_decisions=approval_point.allowed_decisions,
            required_scope=approval_point.required_scope,
            status=WorkflowApprovalStatus.PENDING,
            requested_at=requested_at,
            expires_at=requested_at + timedelta(seconds=record.approval_timeout_seconds),
        )
        saved, created = await asyncio.to_thread(self.repository.ensure_approval, approval)
        interrupted, changed = await asyncio.to_thread(
            self.repository.set_status, record.run_id, WorkflowRunStatus.INTERRUPTED
        )
        if created or changed:
            await self._audit(
                interrupted,
                AuditEventType.WORKFLOW_INTERRUPTED,
                decision="approval_requested",
                payload_hash=record.arguments_hash,
                resource_id=saved.approval_id,
                resource_type="workflow_approval",
            )
        return RunStatusResponse(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=WorkflowRunStatus.INTERRUPTED.value,
            output={
                "approval": saved.request_payload,
                "expires_at": _aware(saved.expires_at).isoformat(),
            },
        )

    async def _complete(
        self, record: WorkflowRun, raw_output: Mapping[str, Any]
    ) -> RunStatusResponse:
        output = dict(raw_output)
        if isinstance(output.get("response"), Mapping):
            output = dict(output["response"])
        try:
            definition = self.catalog[(record.workflow_id, record.workflow_version)]
            validated = definition.output_schema.model_validate(output).model_dump(mode="json")
            if (
                validated.get("workflow_id") != record.workflow_id
                or validated.get("workflow_version") != record.workflow_version
                or validated.get("run_id") != record.run_id
                or validated.get("arguments_hash") != record.arguments_hash
            ):
                raise ValueError("workflow output does not match its pinned business run")
        except Exception as exc:
            failed, changed = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.FAILED
            )
            if changed:
                await self._audit(
                    failed,
                    AuditEventType.WORKFLOW_FAILED,
                    decision="invalid_output",
                    payload_hash=record.arguments_hash,
                )
            raise WorkflowConflict("workflow returned invalid published output") from exc
        result_status = WorkflowRunStatus(str(validated["status"]))
        artifact = validated.get("artifact")
        artifact_refs = (str(artifact["artifact_id"]),) if isinstance(artifact, Mapping) else ()
        completed, changed = await asyncio.to_thread(
            self.repository.set_status,
            record.run_id,
            result_status,
            output_payload=validated,
            artifact_refs=artifact_refs,
        )
        if changed and result_status is WorkflowRunStatus.COMPLETED:
            await self._audit(
                completed,
                AuditEventType.WORKFLOW_COMPLETED,
                decision="completed",
                payload_hash=_hash(validated),
                artifact_refs=artifact_refs,
            )
        if changed and result_status is WorkflowRunStatus.FAILED:
            await self._audit(
                completed,
                AuditEventType.WORKFLOW_FAILED,
                decision="workflow_failed",
                payload_hash=_hash(validated),
            )
        return self._response(completed)

    async def _owned(self, run_id: str, tenant_id: str, subject_id: str) -> WorkflowRun:
        try:
            return await asyncio.to_thread(self.repository.get_owned, run_id, tenant_id, subject_id)
        except WorkflowNotFound as exc:
            raise RunNotFound(str(exc)) from exc

    async def _audit(
        self,
        record: WorkflowRun,
        event: AuditEventType,
        *,
        decision: str,
        payload_hash: str,
        resource_id: str | None = None,
        resource_type: str = "workflow",
        artifact_refs: tuple[str, ...] = (),
    ) -> None:
        audit = AuditRecord(
            event_type=event,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            conversation_id=None,
            turn_id=self._turn_id(record),
            run_id=record.run_id,
            resource_type=resource_type,
            resource_id=resource_id or record.workflow_id,
            resource_version=record.workflow_version,
            action="execute" if resource_type == "workflow" else "approve",
            decision=decision,
            policy_version="workflow-policy/1.0.0",
            payload_hash=payload_hash,
            artifact_refs=artifact_refs,
            metadata={
                "assistant_id": record.assistant_id,
                "deployment_revision": record.deployment_revision,
                "model_profile_id": record.model_profile_id,
            },
        )
        await asyncio.to_thread(self.audit.append, audit)

    @staticmethod
    def _context(record: WorkflowRun, scopes: frozenset[str]) -> ExecutionContext:
        return ExecutionContext(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
            turn_id=WorkflowService._turn_id(record),
            run_id=record.run_id,
        )

    @staticmethod
    def _turn_id(record: WorkflowRun) -> str:
        return f"workflow-{record.run_id.removeprefix('run-')}"

    @staticmethod
    def _metadata(record: WorkflowRun, context: ExecutionContext) -> dict[str, Any]:
        metadata = {
            **context.trace_metadata(),
            "stage": "4",
            "target_kind": "workflow",
            "workflow_id": record.workflow_id,
            "workflow_version": record.workflow_version,
            "deployment_revision": record.deployment_revision,
            "model_profile_id": record.model_profile_id,
            "arguments_hash": record.arguments_hash,
        }
        metadata["application_run_id"] = metadata.pop("run_id")
        return metadata

    @staticmethod
    def _require_scopes(granted: frozenset[str], required: frozenset[str]) -> None:
        if "*" not in granted and not required.issubset(granted):
            raise WorkflowAuthorizationError("required workflow scope is missing")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("workflow clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _response(record: WorkflowRun) -> RunStatusResponse:
        return RunStatusResponse(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status.value,
            output=record.output_payload,
        )


_TERMINAL = {
    WorkflowRunStatus.COMPLETED,
    WorkflowRunStatus.REJECTED,
    WorkflowRunStatus.FAILED,
}


def _server_status(value: str) -> WorkflowRunStatus:
    if value == "running":
        return WorkflowRunStatus.RUNNING
    return WorkflowRunStatus.PENDING


def _aware(value: datetime) -> datetime:
    # SQLite drops the timezone marker; all application timestamps are UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hash(value: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _interrupt_payload(server: Mapping[str, Any]) -> dict[str, Any]:
    interrupts = server.get("interrupts", server.get("__interrupt__"))
    if not isinstance(interrupts, list | tuple) or not interrupts:
        raise WorkflowConflict("workflow interruption is missing approval payload")
    first = interrupts[0]
    if hasattr(first, "value"):
        value = first.value
    elif isinstance(first, Mapping):
        value = first.get("value", first)
    else:
        value = None
    if not isinstance(value, Mapping):
        raise WorkflowConflict("workflow approval payload must be an object")
    required = {
        "approval_id",
        "approval_point",
        "workflow_id",
        "workflow_version",
        "requested_action",
        "arguments_hash",
        "allowed_decisions",
        "required_scope",
    }
    if not required.issubset(value):
        raise WorkflowConflict("workflow approval payload is incomplete")
    return dict(value)
