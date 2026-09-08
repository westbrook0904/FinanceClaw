"""后台任务的交互受理：唯一决定、授权依据与恢复命令同事务提交。"""

import asyncio

from pydantic import ValidationError

from financeclaw.coordination.interactions.repository import (
    InteractionConflict,
    InteractionRepository,
)
from financeclaw.coordination.repository import aware, now
from financeclaw.kernel.coordination import InteractionRequest, ResponseDelivery
from financeclaw.shared.execution_ledger.authorization import (
    check_authorization,
    intersect_scopes,
    require_scopes,
)
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import digest
from financeclaw.shared.execution_ledger.tables import RunExecutionRow
from financeclaw.shared.infrastructure.security.redaction import redact_sensitive


class ReadOnlyInteractionRepository(InteractionRepository):
    """查询不惰性更新过期状态，过期处理由有持久责任的 Worker 进行。"""

    def get_owned(self, *args, **kwargs):
        """保留已有 HTTP 和渠道查询契约，但强制纯读。"""
        kwargs["read_only"] = True
        return super().get_owned(*args, **kwargs)


class CoordinatorInteractions:
    """HTTP、兼容审批入口与飞书复用同一决定；不发送 start/resume。"""

    def __init__(self, admission):
        """依赖共享受理服务与正式交互仓储。"""
        self.admission = admission
        self.store = admission.store
        self.repository = ReadOnlyInteractionRepository(self.store.execution)
        self.clock = now

    async def public(self, row):
        """只返回受治理的交互投影，不输出原生等待位置或用户回答。"""
        row = await asyncio.to_thread(
            self.repository.get_owned,
            row["interaction_id"],
            row["tenant_id"],
            row["subject_id"],
            now=now(),
        )
        if "coordination" not in row["request"]:
            return {
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
            } | {"waiting_reason": "legacy_migration_required"}
        request = InteractionRequest.model_validate(row["request"]["coordination"])
        projection = {
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
        projection.update(
            expires_at=request.expires_at.isoformat(),
            response_url=f"/v1/interactions/{request.request_id}/responses",
        )
        if request.point.kind == "input":
            projection["response_schema"] = request.point.response_schema
        elif request.point.kind == "choice":
            projection["options"] = list(request.point.options)
        else:
            projection.update(
                action_hash=request.action_hash,
                arguments_hash=request.action_hash,
                allowed_decisions=list(request.allowed_decisions),
                action=redact_sensitive(request.action),
                interrupt_id=row["interaction_id"],
            )
        if row["operation_id"]:
            operation = await asyncio.to_thread(self.store.execution.operation, row["operation_id"])
            projection["resume_status"] = operation["status"]
        return projection

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
        """原版本、schema、动作、owner、grant、唯一操作与唤醒一次提交。"""
        from financeclaw.coordination.application.admission import bounded_authorization

        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise InteractionConflict("bounded response idempotency key is required")
        evidence, expires = bounded_authorization(
            self.admission.settings,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            evidence=authorization,
        )

        def accept():
            """只受理原请求的合法决定，幂等重放不刷新 grant 或命令。"""
            saved = self.repository.get_owned(interaction_id, tenant_id, subject_id, now=now())
            if conversation_id is not None and saved["conversation_id"] != conversation_id:
                raise InteractionConflict("interaction belongs to another channel conversation")
            if "coordination" not in saved["request"]:
                raise InteractionConflict("legacy interaction requires explicit migration")
            request = InteractionRequest.model_validate(saved["request"]["coordination"])
            try:
                command = ResponseDelivery(
                    operation_id="operation-"
                    + digest([saved["owner_run_id"], "interaction:" + interaction_id]),
                    request=request,
                    response=response,
                )
            except ValidationError:
                raise InteractionConflict(
                    "response does not match the frozen interaction"
                ) from None
            with self.store.sessions.begin() as session:
                row = self.store.lock(session, saved["root_run_id"])
                is_new = session.get(PendingInteractionRow, interaction_id).response is None
                owner = session.get(RunExecutionRow, saved["owner_run_id"])
                required = self.admission.releases.verify(owner.snapshot).required_scopes
                effective = intersect_scopes(owner.snapshot["context"]["scopes"], scopes)
                require_scopes(effective, required)
                if request.point.required_scope:
                    require_scopes(scopes, {request.point.required_scope})
                if is_new:
                    grant = check_authorization(
                        session, session.get(RunExecutionRow, row.run_id), scopes=effective
                    )
                    grant.scopes = sorted(effective)
                    grant.expires_at = min(aware(grant.expires_at), expires)
                    grant.source, grant.source_hash = evidence.source, evidence.source_hash
                    grant.issued_at, grant.revision = evidence.issued_at, grant.revision + 1
                    self.store.authorization_event(session, row, grant, "decision_accepted")
                operation = {
                    "kind": "response",
                    "payload": command.model_dump(mode="json"),
                    "predecessor": request.source_execution_ref.operation_id,
                    "scopes": sorted(effective),
                    "authorization": evidence.model_dump(mode="json"),
                    "authorization_expires_at": expires.isoformat(),
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
                    self.store.command_inbox(session, row, command.operation_id)
                    self.store.project(
                        session,
                        row,
                        status="running",
                        waiting_reason="resume_pending",
                        pending_interactions=[],
                    )
                return decided

        decided = await asyncio.to_thread(accept)
        return await self.public(decided)
