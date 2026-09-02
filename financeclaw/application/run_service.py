"""BFF run dispatch, ownership, idempotency and approval mapping."""

import json
from asyncio import Lock
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any
from uuid import uuid4

from financeclaw.contracts import (
    ApprovalDecision,
    ApprovalDecisionType,
    ExecutionContext,
    RunAccepted,
    RunRequest,
    RunStatusResponse,
    StreamEvent,
)

from .agent_server_client import AgentServerClient
from .target_resolver import ResolvedTarget, TargetResolver


class IdempotencyConflict(RuntimeError):
    pass


class RunNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    server_run_id: str
    thread_id: str
    tenant_id: str
    subject_id: str
    assistant_id: str
    target_kind: str
    target_id: str
    target_version: str
    fingerprint: str
    context: ExecutionContext
    status: str


class RunService:
    """Thin mapping layer; it never implements a run state machine or queue."""

    def __init__(self, client: AgentServerClient, resolver: TargetResolver) -> None:
        self.client = client
        self.resolver = resolver
        self._by_idempotency: dict[tuple[str, str, str], RunRecord] = {}
        self._by_run_id: dict[str, RunRecord] = {}
        self._lock = Lock()

    @staticmethod
    def _fingerprint(request: RunRequest) -> str:
        payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode()).hexdigest()

    async def start(
        self,
        request: RunRequest,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
    ) -> RunAccepted:
        fingerprint = self._fingerprint(request)
        key = (tenant_id, subject_id, idempotency_key)
        async with self._lock:
            existing = self._by_idempotency.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used for another request"
                    )
                return self._accepted(existing, replay=True)
            resolved = self.resolver.resolve(request)
            record = await self._create_record(
                resolved,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
                fingerprint=fingerprint,
            )
            self._by_idempotency[key] = record
            self._by_run_id[record.run_id] = record
            return self._accepted(record, replay=False)

    async def _create_record(
        self,
        target: ResolvedTarget,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        fingerprint: str,
    ) -> RunRecord:
        run_id = f"run-{uuid4().hex}"
        thread_id = f"thread-{uuid4().hex}"
        context = ExecutionContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            turn_id=f"turn-{uuid4().hex}",
            run_id=run_id,
        )
        metadata = {
            **context.trace_metadata(),
            "stage": "1",
            "target_kind": target.kind,
            "target_id": target.target_id,
            "target_version": target.target_version,
        }
        await self.client.create_thread(thread_id)
        server_run = await self.client.create_run(
            thread_id=thread_id,
            assistant_id=target.assistant_id,
            input=target.input,
            context=context.model_dump(mode="json"),
            metadata=metadata,
        )
        return RunRecord(
            run_id=run_id,
            server_run_id=server_run.run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            assistant_id=target.assistant_id,
            target_kind=target.kind,
            target_id=target.target_id,
            target_version=target.target_version,
            fingerprint=fingerprint,
            context=context,
            status=server_run.status,
        )

    async def status(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        result = await self.client.get_run(
            thread_id=record.thread_id,
            run_id=record.server_run_id,
        )
        status = str(result.get("status", record.status))
        self._by_run_id[run_id] = replace(record, status=status)
        output = result.get("output")
        if not isinstance(output, dict | list):
            output = None
        return RunStatusResponse(
            run_id=run_id,
            thread_id=record.thread_id,
            status=status,
            output=output,
        )

    async def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> RunStatusResponse:
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        mapped: dict[str, Any] = {"type": decision.type.value}
        if decision.arguments_hash is not None:
            mapped["arguments_hash"] = decision.arguments_hash
        if decision.reason is not None:
            mapped["message"] = decision.reason
        if decision.type is ApprovalDecisionType.EDIT:
            mapped["edited_action"] = {
                "name": record.target_id,
                "args": decision.arguments,
            }
        result = await self.client.resume_run(
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
            command={"resume": {"decisions": [mapped]}},
            context=record.context.model_dump(mode="json"),
            metadata={**record.context.trace_metadata(), "stage": "1"},
        )
        interrupted = bool(result.get("__interrupt__"))
        status = "interrupted" if interrupted else "completed"
        output: dict[str, Any] | list[Any] | None = dict(result)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=record.thread_id,
            status=status,
            output=output,
        )

    async def stream(
        self, run_id: str, *, tenant_id: str, subject_id: str
    ) -> AsyncIterator[StreamEvent]:
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        async for part in self.client.stream_thread(
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
        ):
            if isinstance(part, Mapping):
                event = str(part.get("event", "message"))
                data = part.get("data", dict(part))
            else:
                event = str(getattr(part, "event", "message"))
                data = getattr(part, "data", repr(part))
            yield StreamEvent(event=event, data=data)

    def _owned_record(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunRecord:
        record = self._by_run_id.get(run_id)
        if record is None or record.tenant_id != tenant_id or record.subject_id != subject_id:
            raise RunNotFound("run was not found for authenticated owner")
        return record

    @staticmethod
    def _accepted(record: RunRecord, *, replay: bool) -> RunAccepted:
        return RunAccepted(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status,
            target_kind=record.target_kind,
            idempotent_replay=replay,
        )
