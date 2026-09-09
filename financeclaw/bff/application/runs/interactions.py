"""BFF human decisions and their immutable root resume command share one transaction."""

import asyncio

from jsonschema import Draft202012Validator

from financeclaw.bff.application.runs.waits import native_response
from financeclaw.kernel.interactions import InteractionPoint
from financeclaw.shared.execution_ledger.authorization import (
    check_authorization,
    intersect_scopes,
    require_scopes,
)
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.interactions import (
    InteractionConflict,
    InteractionRepository,
)
from financeclaw.shared.execution_ledger.root_repository import aware, now
from financeclaw.shared.execution_ledger.tables import RunExecutionRow
from financeclaw.shared.infrastructure.security.redaction import redact_sensitive


def public_interaction(row):
    """Return governed questions/actions only, without raw native checkpoint or answer."""
    request = row["request"]["bff"]
    point = request["point"]
    result = {
        key: row[key]
        for key in (
            "interaction_id",
            "revision",
            "kind",
            "status",
            "question",
            "root_run_id",
            "owner_run_id",
        )
    }
    result.update(
        expires_at=request["expires_at"],
        response_url=f"/v1/interactions/{row['interaction_id']}/responses",
    )
    if point["kind"] == "input":
        result["response_schema"] = point["response_schema"]
    elif point["kind"] == "choice":
        result["options"] = point["options"]
    else:
        result.update(
            action_hash=request["action_hash"],
            arguments_hash=request["action_hash"],
            allowed_decisions=request["allowed_decisions"],
            action=redact_sensitive(request["action"]),
            interrupt_id=row["interaction_id"],
        )
    return result


class BFFInteractions:
    """All response channels converge on one revision, one decision and one resume operation."""

    def __init__(self, service):
        """Use the same transaction-capable execution and Journal store as admission."""
        self.service, self.store = service, service.store
        self.repository = InteractionRepository(self.store.execution)
        self.clock = now

    async def public(self, row):
        """Reload a safe projection without modifying the stored interaction."""
        saved = await asyncio.to_thread(
            self.repository.get_owned,
            row["interaction_id"],
            row["tenant_id"],
            row["subject_id"],
            now=now(),
        )
        result = public_interaction(saved)
        if saved["operation_id"]:
            operation = await asyncio.to_thread(
                self.store.execution.operation, saved["operation_id"]
            )
            result["resume_status"] = operation["status"]
        return result

    async def respond(
        self,
        interaction_id,
        response,
        *,
        tenant_id,
        subject_id,
        scopes,
        idempotency_key,
        conversation_id=None,
        authorization=None,
    ):
        """Validate the fixed schema and authority, then persist a recoverable human command."""
        from financeclaw.bff.application.runs.service import bounded_authorization

        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise InteractionConflict("bounded response idempotency key is required")
        evidence, expires = bounded_authorization(
            self.service.settings,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            evidence=authorization,
        )

        def accept():
            """Serialize answers, attempts, cancellation and finalization under the root lock."""
            saved = self.repository.get_owned(interaction_id, tenant_id, subject_id, now=now())
            if conversation_id is not None and conversation_id != saved["conversation_id"]:
                raise InteractionConflict("interaction belongs to another channel conversation")
            request = saved["request"]["bff"]
            point = InteractionPoint.model_validate(request["point"])
            if (
                response.revision != saved["revision"]
                or response.kind != point.kind
                or response.action_hash != request["action_hash"]
                or (
                    point.kind == "input"
                    and not Draft202012Validator(point.response_schema).is_valid(response.answer)
                )
                or (point.kind == "choice" and response.answer not in point.options)
                or (
                    point.kind == "approval"
                    and response.decision not in request["allowed_decisions"]
                )
            ):
                raise InteractionConflict("response does not match the frozen interaction")
            with self.store.sessions.begin() as session:
                root = self.store.lock(session, saved["root_run_id"])
                execution = session.get(RunExecutionRow, root.run_id)
                profile = self.service.releases.verify(execution.snapshot)
                effective = intersect_scopes(execution.snapshot["context"]["scopes"], scopes)
                require_scopes(effective, profile.required_scopes)
                if point.required_scope:
                    require_scopes(scopes, {point.required_scope})
                is_new = session.get(PendingInteractionRow, interaction_id).response is None
                if is_new:
                    if not root.active:
                        raise InteractionConflict("terminal root cannot accept an answer")
                    grant = check_authorization(session, execution, scopes=effective)
                    grant.scopes = sorted(effective)
                    grant.expires_at = min(aware(grant.expires_at), expires)
                    grant.source, grant.source_hash = evidence.source, evidence.source_hash
                    grant.issued_at, grant.revision = evidence.issued_at, grant.revision + 1
                    self.store.authorization_event(session, root, grant, "decision_accepted")
                operation = {
                    "kind": "resume",
                    "predecessor": saved["server_run_id"],
                    "scopes": sorted(effective),
                    "payload": {
                        "interaction_id": interaction_id,
                        "revision": response.revision,
                        "binding": request["binding"],
                        "expires_at": request["expires_at"],
                        "response": native_response(request, response),
                    },
                }
                decided = self.repository.decide(
                    interaction_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    revision=response.revision,
                    response_key=idempotency_key,
                    response=response.model_dump(mode="json"),
                    operation=operation,
                    now=now(),
                    session=session,
                )
                if is_new:
                    self.store.command_inbox(session, root, decided["operation_id"])
                    self.store.project(
                        session,
                        root,
                        status="running",
                        waiting_reason="resume_pending",
                        pending_interactions=[],
                    )
                return decided

        decided = await asyncio.to_thread(accept)
        if self.service.lifecycle:
            self.service.lifecycle.wake()
        return await self.public(decided)
