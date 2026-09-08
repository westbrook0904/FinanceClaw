"""同库受理 Facade：只提交业务事实，不在 BFF 请求栈中调用 backend。"""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from financeclaw.coordination.application.releases import release_ref
from financeclaw.coordination.application.run_service import IdempotencyConflict, RunNotFound
from financeclaw.coordination.repository import DRIVER_VERSION, now
from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.coordination import TaskSubmission, bounded_digest
from financeclaw.kernel.responses import RunAccepted, RunStatusResponse, StreamEvent
from financeclaw.shared.conversation.repository import (
    IdempotencyConflict as JournalIdempotencyConflict,
)
from financeclaw.shared.execution_ledger.authorization import intersect_scopes, require_scopes
from financeclaw.shared.execution_ledger.coordination_tables import (
    CoordinatedRunRow,
    RunAuthorizationRow,
)
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.snapshots import agent_snapshot
from financeclaw.shared.execution_ledger.tables import RunExecutionRow


def bounded_authorization(settings, *, tenant_id, subject_id, scopes, evidence):
    """可信来源和期限固定；开发调用可使用显式开发适配器，正式环境必须带依据。"""
    current = now()
    if evidence is None:
        if settings.environment.value not in {"development", "test"}:
            raise ExecutionConflict("verified background authorization evidence is required")
        evidence = AuthorizationEvidence(
            source="development",
            source_hash=digest([tenant_id, subject_id, sorted(scopes)]),
            issued_at=current,
            expires_at=current + timedelta(seconds=settings.coordinator_grant_seconds),
        )
    if (
        evidence.issued_at > current + timedelta(seconds=5)
        or evidence.expires_at <= current
        or (
            evidence.source == "development"
            and settings.environment.value not in {"development", "test"}
        )
    ):
        raise ExecutionConflict("background authorization evidence is invalid or expired")
    return evidence, min(
        evidence.expires_at, current + timedelta(seconds=settings.coordinator_grant_seconds)
    )


def prepare_task(store, session, root, snapshot, payload, scopes):
    """业务输入与权限固定后保存一次 start 命令，不分配远程提交重试键。"""
    context = snapshot_context(snapshot)
    operation_id = "operation-" + digest([context.run_id, "start"])
    command = TaskSubmission(
        task_id=context.run_id,
        root_task_id=root.run_id,
        operation_id=operation_id,
        backend_instance_id=root.backend_instance_id,
        release=release_ref(snapshot),
        input=payload,
        input_hash=bounded_digest(payload),
    )
    store.execution.prepare(
        operation_id,
        context.run_id,
        {"kind": "start", "payload": command.model_dump(mode="json"), "scopes": sorted(scopes)},
        session=session,
    )
    store.command_inbox(session, root, operation_id)
    return command


class CoordinatorAdmission:
    """BFF 的公开应用接口：原子受理、纯查询、用户决定和取消命令。"""

    coordinated = True

    def __init__(self, store, releases, settings):
        """只依赖数据库与发布声明，不装配 Worker 或 HTTP backend。"""
        from financeclaw.coordination.application.decisions import CoordinatorInteractions

        self.store, self.releases, self.settings = store, releases, settings
        self.repository = store.journal
        self.execution = store.execution
        self.interactions = CoordinatorInteractions(self)

    async def start_turn(
        self,
        conversation_id,
        request,
        *,
        tenant_id,
        subject_id,
        scopes,
        idempotency_key,
        authorization=None,
        notification_address=None,
    ):
        """202 只在 Turn、Journal、快照、grant、命令、Inbox 与进度共同提交后返回。"""
        return await asyncio.to_thread(
            self._admit,
            conversation_id,
            request,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            idempotency_key=idempotency_key,
            authorization=authorization,
            notification_address=notification_address,
        )

    def _admit(
        self,
        conversation_id,
        request,
        *,
        tenant_id,
        subject_id,
        scopes,
        idempotency_key,
        authorization,
        notification_address=None,
    ):
        """无 I/O 的受理事务；幂等重放不更新原输入、发布或授权。"""
        conversation = self.repository.get_owned(conversation_id, tenant_id, subject_id)
        profile = self.releases.agents.resolve(
            conversation.agent_id, conversation.agent_profile_version
        )
        require_scopes(scopes, profile.required_scopes)
        evidence, expires_at = bounded_authorization(
            self.settings,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            evidence=authorization,
        )
        request_hash = digest(
            {
                "conversation_id": conversation_id,
                "message": request.message,
                "agent_id": profile.agent_id,
                "agent_profile_version": profile.version,
            }
        )
        try:
            with self.store.sessions.begin() as session:
                turn, _, replay = self.repository.begin_turn(
                    conversation_id=conversation_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    message=request.message,
                    target_type="agent",
                    target_id=profile.agent_id,
                    target_version=profile.version,
                    session=session,
                )
                if replay:
                    row = session.get(CoordinatedRunRow, turn.run_id)
                    if row is None:
                        raise ExecutionConflict("existing legacy turn requires explicit migration")
                    snapshot = session.get(RunExecutionRow, turn.run_id).snapshot
                    if notification_address is not None:
                        from financeclaw.shared.notifications.facts import bind_target

                        bind_target(
                            session,
                            row,
                            notification_address,
                            tenant_id=tenant_id,
                            subject_id=subject_id,
                            evidence=evidence,
                            replay=True,
                        )
                else:
                    context = ExecutionContext(
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        scopes=scopes,
                        conversation_id=conversation_id,
                        turn_id=turn.turn_id,
                        run_id=turn.run_id,
                        root_run_id=turn.run_id,
                        request_clock=now().isoformat(),
                        data_classification=profile.data_classification,
                    )
                    snapshot = agent_snapshot(
                        profile,
                        context,
                        thread_id=conversation.agent_thread_id,
                        input_hash=request_hash,
                    )
                    snapshot.update(
                        driver_mode="coordinator",
                        backend_instance_id=self.store.backend_instance_id,
                    )
                    self.execution.register(turn.run_id, snapshot, session=session)
                    row = CoordinatedRunRow(
                        run_id=turn.run_id,
                        conversation_id=conversation_id,
                        backend_instance_id=self.store.backend_instance_id,
                        driver_version=DRIVER_VERSION,
                        revision=0,
                        projection={},
                        wake=1,
                    )
                    session.add(row)
                    session.add(
                        RunAuthorizationRow(
                            run_id=turn.run_id,
                            scopes=sorted(scopes),
                            source=evidence.source,
                            source_hash=evidence.source_hash,
                            issued_at=evidence.issued_at,
                            expires_at=expires_at,
                        )
                    )
                    session.flush()
                    if notification_address is not None:
                        if not self.settings.feishu_notifications_enabled:
                            raise ExecutionConflict("notification admission is disabled")
                        from financeclaw.shared.notifications.facts import bind_target

                        bind_target(
                            session,
                            row,
                            notification_address,
                            tenant_id=tenant_id,
                            subject_id=subject_id,
                            evidence=evidence,
                        )
                    self.store.authorization_event(
                        session, row, session.get(RunAuthorizationRow, turn.run_id), "admitted"
                    )
                    prepare_task(
                        self.store,
                        session,
                        row,
                        snapshot,
                        {"messages": [{"role": "user", "content": request.message}]},
                        scopes,
                    )
                    self.store.project(
                        session,
                        row,
                        run_id=turn.run_id,
                        thread_id=snapshot["thread_id"],
                        status="accepted",
                        waiting_reason=None,
                        pending_interactions=[],
                    )
                return RunAccepted(
                    run_id=turn.run_id,
                    thread_id=snapshot["thread_id"],
                    status=row.projection["status"],
                    target_kind="agent",
                    idempotent_replay=replay,
                    conversation_id=conversation_id,
                    turn_id=turn.turn_id,
                )
        except JournalIdempotencyConflict as exc:
            raise IdempotencyConflict(str(exc)) from exc

    def root_for_task(self, run_id, *, tenant_id, subject_id):
        """用户可查询归属相同的根和 child，未知对象不暴露其他租户事实。"""
        with self.store.sessions() as session:
            execution = session.get(RunExecutionRow, run_id)
            if execution is None:
                raise RunNotFound("run not found")
            context = snapshot_context(execution.snapshot)
            row = session.get(CoordinatedRunRow, execution.root_run_id)
            if row is None or (context.tenant_id, context.subject_id) != (tenant_id, subject_id):
                raise RunNotFound("run not found")
            return row.run_id

    def manages(self, run_id) -> bool:
        """仅用于 BFF 路由选择；实际读取仍必须做归属检查。"""
        with self.store.sessions() as session:
            execution = session.get(RunExecutionRow, run_id)
            return bool(execution and execution.snapshot.get("driver_mode") == "coordinator")

    async def healthy(self) -> bool:
        """BFF 就绪状态包括兼容 Worker 的独立心跳。"""
        return await asyncio.to_thread(
            self.store.worker_available,
            maximum_age=max(10, self.settings.coordinator_lease_seconds),
        )

    def notification_mode(self, run_id, *, tenant_id, subject_id):
        """目标模式来自首次受理事实，开关关闭后也不能切回另一最终发送路径。"""
        from financeclaw.shared.notifications.tables import NotificationTargetRow

        root_id = self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)
        with self.store.sessions() as session:
            target = session.scalar(
                select(NotificationTargetRow).where(NotificationTargetRow.run_id == root_id)
            )
            return target.delivery_mode if target else None

    def notifications(self, run_id, *, tenant_id, subject_id, revoke=False):
        """按根归属读取投递责任或显式撤销订阅，不暴露目的地址和正文。"""
        from financeclaw.shared.notifications.tables import (
            NotificationDeliveryRow,
            NotificationEventRow,
            NotificationTargetRow,
        )

        root_id = self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)
        if root_id != run_id:
            raise RunNotFound("notification subscription belongs to root task")
        with self.store.sessions.begin() as session:
            statement = select(NotificationTargetRow).where(NotificationTargetRow.run_id == root_id)
            target = session.scalar(statement.with_for_update() if revoke else statement)
            if target is None:
                return {"run_id": root_id, "subscribed": False, "deliveries": []}
            if revoke:
                target.active = False
            events = list(
                session.scalars(
                    select(NotificationEventRow)
                    .where(NotificationEventRow.target_id == target.target_id)
                    .order_by(NotificationEventRow.revision)
                )
            )
            deliveries = list(
                session.scalars(
                    select(NotificationDeliveryRow)
                    .join(NotificationEventRow)
                    .where(NotificationEventRow.target_id == target.target_id)
                    .order_by(NotificationEventRow.revision, NotificationDeliveryRow.part)
                )
            )
            return {
                "run_id": root_id,
                "subscribed": True,
                "active": target.active,
                "delivery_mode": target.delivery_mode,
                "unmaterialized_events": sum(event.materialized_at is None for event in events),
                "deliveries": [
                    {
                        "delivery_id": row.delivery_id,
                        "part": row.part + 1,
                        "parts": row.parts,
                        "status": row.status,
                        "uncertain": row.uncertain,
                        "attempts": row.attempts,
                        "error_class": row.error_class,
                    }
                    for row in deliveries
                ],
            }

    async def status(self, run_id, *, tenant_id, subject_id, scopes=None, **_):
        """GET 只读取数据库投影，不触发 backend、过期写入或业务推进。"""

        def read():
            """同一只读 Session 组合根投影与 Journal。"""
            from financeclaw.shared.conversation.tables import (
                ConversationMessageRow,
                ConversationTurnRow,
            )
            from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
            from financeclaw.shared.execution_ledger.workflow_tables import WorkflowRunRow

            with self.store.sessions() as session:
                execution = session.get(RunExecutionRow, run_id)
                if execution is None:
                    raise RunNotFound("run not found")
                context = snapshot_context(execution.snapshot)
                if (context.tenant_id, context.subject_id) != (tenant_id, subject_id):
                    raise RunNotFound("run not found")
                root_id = execution.root_run_id
                row = session.get(CoordinatedRunRow, root_id)
                if row is not None and run_id != root_id:
                    child = session.scalar(
                        select(DelegationRow).where(DelegationRow.child_run_id == run_id)
                    )
                    if child is None:
                        raise RunNotFound("child run not found")
                    child_status = child.execution_status
                    terminal = child_status in {"completed", "rejected", "failed"}
                    if not terminal and execution.cancellation_requested:
                        child_status = (
                            "cancelled"
                            if execution.cancellation_confirmed
                            else "cancellation_requested"
                        )
                    interactions = [
                        item
                        for item in row.projection.get("pending_interactions", ())
                        if item["owner_run_id"] == run_id
                    ]
                    return RunStatusResponse(
                        run_id=run_id,
                        thread_id=execution.snapshot["thread_id"],
                        status=child_status,
                        output=child.output_payload if terminal else None,
                        waiting_reason=None if terminal else row.projection.get("waiting_reason"),
                        pending_interactions=tuple(interactions),
                    )
                turn = session.scalar(
                    select(ConversationTurnRow).where(ConversationTurnRow.run_id == root_id)
                )
                if row is None:
                    workflow = session.get(WorkflowRunRow, run_id)
                    if workflow and workflow.status in {
                        "completed",
                        "rejected",
                        "failed",
                        "cancelled",
                    }:
                        return RunStatusResponse(
                            run_id=run_id,
                            thread_id=workflow.thread_id,
                            status=workflow.status,
                            output=workflow.output_payload,
                        )
                    terminal = turn and turn.status in {"completed", "failed", "cancelled"}
                    projection = {
                        "run_id": root_id,
                        "thread_id": execution.snapshot["thread_id"],
                        "status": turn.status if terminal else "interrupted",
                        "waiting_reason": None if terminal else "legacy_migration_required",
                    }
                else:
                    projection = dict(row.projection)
                if turn and projection["status"] == "completed":
                    content = session.scalar(
                        select(ConversationMessageRow.content).where(
                            ConversationMessageRow.turn_id == turn.turn_id,
                            ConversationMessageRow.role == "assistant",
                            ConversationMessageRow.parent_message_id.is_(None),
                        )
                    )
                    projection["output"] = {"messages": [{"type": "assistant", "content": content}]}
                return RunStatusResponse.model_validate(projection)

        return await asyncio.to_thread(read)

    def assert_owned(self, run_id, *, tenant_id, subject_id):
        """供现有渠道校验归属，不执行后台推进。"""
        self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def assistant_content(self, run_id, *, tenant_id, subject_id):
        """最终结果从唯一 Journal 读取，child 文本不成为根最终答案。"""
        root_id = await asyncio.to_thread(
            self.root_for_task, run_id, tenant_id=tenant_id, subject_id=subject_id
        )
        turn = await asyncio.to_thread(
            self.repository.get_turn_owned, root_id, tenant_id, subject_id
        )
        messages = await asyncio.to_thread(self.repository.list_messages, turn.conversation_id)
        return next(
            (
                message.content
                for message in messages
                if message.turn_id == turn.turn_id and message.role.value == "assistant"
            ),
            None,
        )

    async def cancel(self, run_id, *, tenant_id, subject_id):
        """本地取消仅封闭派发并唤醒；确切停止由独立 Worker 确认。"""

        def accept():
            """根锁内受理取消，不等待 backend 停止。"""
            root_id = self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)
            with self.store.sessions.begin() as session:
                row = self.store.lock(session, root_id)
                if row.active:
                    self.interactions.repository.cancel_tree(root_id, now=now(), session=session)
                    self.repository.update_turn_status(
                        root_id, "cancellation_requested", session=session
                    )
                    self.store.command_inbox(session, row, "cancel")
                    self.store.project(
                        session,
                        row,
                        status="cancellation_requested",
                        waiting_reason="execution_stop_not_confirmed",
                        pending_interactions=[],
                    )

        await asyncio.to_thread(accept)
        return await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def reauthorize(self, run_id, *, tenant_id, subject_id, scopes, authorization=None):
        """显式更新有限 grant；原输入、发布、request clock 与已受理决定都不改变。"""
        evidence, expires_at = bounded_authorization(
            self.settings,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            evidence=authorization,
        )

        def accept():
            """记录新的有限授权和唤醒，不覆盖任何已冻结业务命令。"""
            root_id = self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)
            with self.store.sessions.begin() as session:
                row = self.store.lock(session, root_id)
                execution = session.get(RunExecutionRow, root_id)
                if not row.active or execution.cancellation_requested:
                    raise ExecutionConflict("terminal or cancelling task cannot be reauthorized")
                grant = session.get(RunAuthorizationRow, root_id)
                grant.scopes = sorted(
                    intersect_scopes(snapshot_context(execution.snapshot).scopes, scopes)
                )
                grant.source, grant.source_hash, grant.issued_at = (
                    evidence.source,
                    evidence.source_hash,
                    evidence.issued_at,
                )
                grant.expires_at, grant.revoked, grant.revision = (
                    expires_at,
                    False,
                    grant.revision + 1,
                )
                self.store.command_inbox(session, row, f"authorize:{grant.revision}")
                self.store.authorization_event(session, row, grant, "reauthorized")

        await asyncio.to_thread(accept)
        return await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def revoke_authorization(self, run_id, *, tenant_id, subject_id):
        """原主体显式撤销本地 grant，后续受治理动作立即拒绝。"""

        def revoke():
            """撤销和可见状态在同一根锁事务提交。"""
            root_id = self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)
            with self.store.sessions.begin() as session:
                row = self.store.lock(session, root_id)
                grant = session.get(RunAuthorizationRow, root_id)
                if not row.active or grant.revoked:
                    return
                grant.revoked, grant.revision = True, grant.revision + 1
                self.store.authorization_event(session, row, grant, "revoked")
                self.store.command_inbox(session, row, f"revoke:{grant.revision}")
                cancelling = session.get(RunExecutionRow, root_id).cancellation_requested
                self.store.project(
                    session,
                    row,
                    status="cancellation_requested" if cancelling else "interrupted",
                    waiting_reason="execution_stop_not_confirmed"
                    if cancelling
                    else "authorization_required",
                )

        await asyncio.to_thread(revoke)
        return await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def resume(self, run_id, decision, *, tenant_id, subject_id, scopes, authorization=None):
        """旧审批路由只受理显式批准／拒绝，映射到同一个交互实例。"""
        from financeclaw.kernel.interactions import InteractionResponse

        root_id = await asyncio.to_thread(
            self.root_for_task, run_id, tenant_id=tenant_id, subject_id=subject_id
        )
        with self.store.sessions() as session:
            rows = list(
                session.scalars(
                    select(PendingInteractionRow).where(
                        PendingInteractionRow.root_run_id == root_id,
                        PendingInteractionRow.status == "pending",
                    )
                )
            )
            if len(rows) != 1 or rows[0].kind != "approval":
                raise ExecutionConflict("run has no single pending approval")
            row = rows[0]
            if decision.type.value not in {"approve", "reject"} or decision.interrupt_id not in {
                row.interrupt_id,
                row.interaction_id,
            }:
                raise ExecutionConflict("approval refers to a different interaction")
            response = InteractionResponse(
                revision=row.revision,
                kind="approval",
                decision=decision.type.value,
                action_hash=decision.arguments_hash,
                reason=decision.reason,
            )
            identifier = row.interaction_id
        await self.interactions.respond(
            identifier,
            response,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            authorization=authorization,
            idempotency_key="legacy:" + digest(response.model_dump(mode="json")),
        )
        return await self.status(root_id, tenant_id=tenant_id, subject_id=subject_id)

    async def stream(
        self, run_id, *, tenant_id, subject_id, scopes=frozenset(), last_event_id=None
    ):
        """根事件使用独立客户端游标；子任务和历史模式保持只读兼容投影。"""
        if (
            self.manages(run_id)
            and self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id) == run_id
        ):
            from financeclaw.coordination.application.progress import stream_progress

            async for event in stream_progress(
                self,
                run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                last_event_id=last_event_id,
            ):
                yield event
        else:
            async for event in self._status_stream(
                run_id, tenant_id=tenant_id, subject_id=subject_id, scopes=scopes
            ):
                yield event

    async def _status_stream(self, run_id, *, tenant_id, subject_id, scopes=frozenset()):
        """有限的只读状态流；重连、断开或无人订阅都不会影响推进责任。"""
        from financeclaw.coordination.application.streaming import (
            completed_stream_event,
            failed_stream_event,
            interrupted_stream_event,
        )

        previous = None
        while True:
            status = await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)
            payload = status.model_dump(mode="json")
            if payload != previous:
                if status.status == "completed":
                    yield completed_stream_event(run_id, status.output)
                elif status.status == "failed":
                    yield failed_stream_event(run_id)
                elif status.status == "interrupted":
                    yield interrupted_stream_event(
                        run_id,
                        waiting_reason=status.waiting_reason,
                        pending_interactions=tuple(status.pending_interactions),
                    )
                else:
                    yield StreamEvent(event="run.progress", data=payload)
                previous = payload
            if status.status in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "cancellation_requested",
            }:
                return
            await asyncio.sleep(self.settings.coordinator_poll_seconds)

    async def reconcile_incomplete(self):
        """BFF 启动不再承担协调补偿；数据库责任由 Worker 持续领取。"""
        return ()
