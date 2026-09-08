"""有界业务推进：命令、观察、显式请求与证据，图执行仍由 Agent backend 拥有。"""

import asyncio
import json

from sqlalchemy import select

from financeclaw.coordination.application.transitions import CoordinationTransitions
from financeclaw.coordination.backends.ports.backend import AgentBackend
from financeclaw.coordination.repository import aware, now
from financeclaw.kernel.coordination import (
    BackendExecutionRef,
    DelegationRequest,
    ResponseDelivery,
    TaskSubmission,
)
from financeclaw.shared.execution_ledger.authorization import check_authorization
from financeclaw.shared.execution_ledger.coordination_tables import (
    BackendAttemptRow,
)
from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow


class Coordinator:
    """一次 advance 执行有限远程步骤；任何步骤都可从同一数据库事实重新进入。"""

    def __init__(self, store, releases, backend: AgentBackend, settings, *, artifacts=None):
        """出站只依赖中立 Backend Port，可用能力受限的 backend 验证边界。"""
        backend.capabilities.require_role("parent")
        self.store, self.releases, self.backend, self.settings = store, releases, backend, settings
        self.transitions = CoordinationTransitions(store, releases)
        self.artifacts = artifacts

    def _read(self, claim):
        """提交本轮过期处理，随后脱离事务执行 HTTP。"""
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            execution = session.get(RunExecutionRow, root.run_id)
            expired = set()
            for pending in session.scalars(
                select(PendingInteractionRow).where(
                    PendingInteractionRow.root_run_id == root.run_id,
                    PendingInteractionRow.status == "pending",
                )
            ):
                if now() >= aware(pending.expires_at):
                    self.transitions.interactions._close(session, pending, "expired", now())
                    expired.add(pending.interaction_id)
            if expired:
                self.store.project(
                    session,
                    root,
                    pending_interactions=[
                        {**item, "status": "expired"} if item["interaction_id"] in expired else item
                        for item in root.projection.get("pending_interactions", ())
                    ],
                )
            operations = [
                dict((column.name, getattr(op, column.name)) for column in op.__table__.columns)
                for op in session.scalars(
                    select(RunOperationRow)
                    .join(RunExecutionRow)
                    .where(RunExecutionRow.root_run_id == root.run_id)
                    .order_by(RunOperationRow.created_at, RunOperationRow.operation_id)
                )
            ]
            try:
                grant = check_authorization(session, execution)
                scopes = frozenset(grant.scopes)
            except ExecutionConflict:
                scopes = None
            return {
                "active": root.active,
                "cancelled": execution.cancellation_requested,
                "operations": operations,
                "scopes": scopes,
            }

    def blocked(self, claim, reason):
        """持久展示待处理原因；不通过异常消息泄露 backend 或用户载荷。"""
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            if root.active:
                cancelling = session.get(RunExecutionRow, root.run_id).cancellation_requested
                self.store.project(
                    session,
                    root,
                    status="cancellation_requested" if cancelling else "interrupted",
                    waiting_reason=reason,
                )

    @staticmethod
    def command(operation):
        """每次出站重新验证冻结 JSON 及契约摘要。"""
        if digest(operation["request"]) != operation["request_hash"]:
            raise ExecutionConflict("persisted operation hash mismatch")
        contract = TaskSubmission if operation["request"]["kind"] == "start" else ResponseDelivery
        return contract.model_validate(operation["request"]["payload"])

    async def advance(self, claim) -> float:
        """不需要 BFF、GET 或 SSE；通知只是缩短下一次精确观察的等待。"""
        state = await asyncio.to_thread(self._read, claim)
        delay = self.settings.coordinator_reconcile_seconds
        if not state["active"]:
            return delay
        # 未知回执先对账，授权失效或取消也不能把未知尝试遗忘。
        unknown = next(
            (
                op
                for op in state["operations"]
                if op["status"] in {"claimed", "uncertain"} and op["server_run_id"] is None
            ),
            None,
        )
        if unknown:
            receipt = await self.backend.lookup_operation(self.command(unknown))
            if receipt.status == "submitted":
                await asyncio.to_thread(self.transitions.bind, claim, receipt.execution_ref)
                return 0
            await asyncio.to_thread(self.blocked, claim, "submission_uncertain")
            return delay
        if state["cancelled"]:
            return await self._cancel(claim)
        if state["scopes"] is None:
            await asyncio.to_thread(self.blocked, claim, "authorization_required")
            return delay
        prepared = next((op for op in state["operations"] if op["status"] == "prepared"), None)
        if prepared:
            command = self.command(prepared)
            snapshot = await asyncio.to_thread(self.store.execution.get, prepared["run_id"])
            self.releases.verify(snapshot["snapshot"])
            if isinstance(command, TaskSubmission):
                self.backend.capabilities.require_role(
                    "parent" if command.task_id == command.root_task_id else "child"
                )
            if await asyncio.to_thread(self.store.claim_operation, claim, prepared["operation_id"]):
                try:
                    receipt = await (
                        self.backend.submit_task(command)
                        if isinstance(command, TaskSubmission)
                        else self.backend.deliver_response(command)
                    )
                except (Exception, asyncio.CancelledError):
                    await asyncio.to_thread(self._uncertain, claim, prepared["operation_id"])
                    raise
                if receipt.status == "submitted":
                    await asyncio.to_thread(self.transitions.bind, claim, receipt.execution_ref)
                else:
                    await asyncio.to_thread(self._uncertain, claim, prepared["operation_id"])
            return 0
        reference = await asyncio.to_thread(self._active_reference, claim)
        if reference is None:
            return delay
        observation = await self.backend.observe_execution(reference)
        if observation.execution_ref != reference:
            raise ExecutionConflict("backend observation refers to another attempt")
        # 先保存真实交付证据，再处理同一恢复尝试提出的下一请求或失败。
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            self.transitions.applied(session, root, observation)
        if observation.status in {"completed", "failed"}:
            if reference.task_id == claim["run_id"]:
                await asyncio.to_thread(self.transitions.root_completed, claim, observation)
            else:
                observation = await self._bounded_child_result(observation)
                await asyncio.to_thread(self.transitions.child_completed, claim, observation)
            return 0
        if observation.status == "waiting":
            if len(observation.requests) != 1:
                raise ExecutionConflict("only one coordination request is supported")
            request = observation.requests[0]
            binding = observation.continuation_bindings.get(
                request.continuation_ref.continuation_id
            )
            if binding is None:
                raise ExecutionConflict("backend omitted durable continuation binding")
            if isinstance(request, DelegationRequest):
                snapshot = await asyncio.to_thread(self.store.execution.get, reference.task_id)
                with self.store.sessions() as session:
                    existing = session.get(DelegationRow, request.request_id)
                    already_known = existing is not None
                if not already_known:
                    child = await asyncio.to_thread(
                        self.releases.child, request, snapshot["snapshot"], state["scopes"]
                    )
                    await asyncio.to_thread(
                        self.transitions.delegate, claim, request, binding, child
                    )
                    return 0
            else:
                await asyncio.to_thread(self.transitions.interaction, claim, request, binding)
            return delay
        if observation.status == "unknown":
            await asyncio.to_thread(self.blocked, claim, "unsupported_interruption")
        else:
            with self.store.sessions.begin() as session:
                root = self.store.lock(session, claim["run_id"], claim)
                self.store.project(
                    session, root, status="running", waiting_reason=None, pending_interactions=[]
                )
        return delay

    def _uncertain(self, claim, operation_id):
        """异常回执记录受 fencing 保护；失效 Worker 留下 claimed 供新 Worker 对账。"""
        with self.store.sessions.begin() as session:
            self.store.lock(session, claim["run_id"], claim)
            self.store.execution.uncertain(operation_id, session=session)

    def _active_reference(self, claim):
        """父仍等待结果时观察唯一 child；恢复提交后观察原 parent 的新尝试。"""
        with self.store.sessions() as session:
            root_id = claim["run_id"]
            pending = session.scalar(
                select(DelegationRow).where(
                    DelegationRow.parent_run_id == root_id, DelegationRow.delivered_at.is_(None)
                )
            )
            task_id = root_id
            if pending and pending.completed_at is None:
                task_id = pending.child_run_id
            execution = session.get(RunExecutionRow, task_id)
            attempt = (
                session.get(BackendAttemptRow, execution.server_run_id)
                if execution and execution.server_run_id
                else None
            )
            return BackendExecutionRef.model_validate(attempt.reference) if attempt else None

    async def _bounded_child_result(self, observation):
        """大结果先幂等写受治理 Artifact，失败不会重跑子 Agent。"""
        if len(json.dumps(observation.result, ensure_ascii=False).encode()) < 10000:
            return observation
        if self.artifacts is None:
            raise ExecutionConflict("large child result requires artifact storage")
        execution = await asyncio.to_thread(
            self.store.execution.get, observation.execution_ref.task_id
        )
        context = snapshot_context(execution["snapshot"])
        metadata = await asyncio.to_thread(
            self.artifacts.persist,
            observation.result,
            context=context,
            source_type="delegation_result",
            source_id=context.run_id,
            idempotency_key=observation.execution_ref.operation_id,
        )
        return observation.model_copy(
            update={
                "result": {
                    "artifact_id": metadata.artifact_id,
                    "content_hash": metadata.content_hash,
                    "size_bytes": metadata.size_bytes,
                }
            }
        )

    async def _cancel(self, claim):
        """逐个停止已登记尝试；全部已知且确认后才关闭根，不伪造未知命令的停止。"""
        with self.store.sessions() as session:
            attempt = session.scalar(
                select(BackendAttemptRow).where(
                    BackendAttemptRow.run_id == claim["run_id"],
                    BackendAttemptRow.cancellation_confirmed.is_(False),
                )
            )
            reference = BackendExecutionRef.model_validate(attempt.reference) if attempt else None
        if reference:
            receipt = await self.backend.request_cancel(
                reference, operation_id="cancel:" + digest(reference.model_dump(mode="json"))
            )
            if receipt.execution_ref != reference:
                raise ExecutionConflict("cancel receipt belongs to another attempt")
            if receipt.status == "confirmed":
                with self.store.sessions.begin() as session:
                    self.store.lock(session, claim["run_id"], claim)
                    session.get(
                        BackendAttemptRow, reference.operation_id
                    ).cancellation_confirmed = True
                return 0
            return self.settings.coordinator_reconcile_seconds
        with self.store.sessions.begin() as session:
            root = self.store.lock(session, claim["run_id"], claim)
            for execution in session.scalars(
                select(RunExecutionRow).where(RunExecutionRow.root_run_id == root.run_id)
            ):
                execution.cancellation_confirmed = True
            self.store.journal.confirm_cancel(root.run_id, session=session)
            root.active = False
            self.store.project(
                session, root, status="cancelled", waiting_reason=None, pending_interactions=[]
            )
        return self.settings.coordinator_reconcile_seconds
