"""BFF product run service: atomic admission, user commands and read-only projections."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from financeclaw.bff.application.runs.store import DRIVER_VERSION
from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.responses import RunAccepted, RunStatusResponse
from financeclaw.kernel.run_errors import IdempotencyConflict, RunNotFound
from financeclaw.shared.conversation.repository import IdempotencyConflict as JournalConflict
from financeclaw.shared.conversation.tables import ConversationRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.authorization import require_scopes
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.root_repository import now
from financeclaw.shared.execution_ledger.run_tables import (
    RootRunRow,
    RunAuthorizationRow,
)
from financeclaw.shared.execution_ledger.snapshots import agent_snapshot
from financeclaw.shared.execution_ledger.tables import RunExecutionRow


def bounded_authorization(settings, *, tenant_id, subject_id, scopes, evidence):
    """Freeze verified finite authorization; synthetic evidence is development/test only."""
    current = now()
    if evidence is None:
        if settings.environment.value not in {"development", "test"}:
            raise ExecutionConflict("verified background authorization evidence is required")
        evidence = AuthorizationEvidence(
            source="development",
            source_hash=digest([tenant_id, subject_id, sorted(scopes)]),
            issued_at=current,
            expires_at=current + timedelta(seconds=settings.bff_run_grant_seconds),
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
        evidence.expires_at, current + timedelta(seconds=settings.bff_run_grant_seconds)
    )


class BFFRunService:
    """One durable root per conversation Turn; only BFF owns native execution commands."""

    def __init__(self, store, releases, settings):
        """Compose persistence and interaction admission."""
        from financeclaw.bff.application.runs.interactions import BFFInteractions

        self.store, self.releases, self.settings = store, releases, settings
        self.repository, self.execution = store.journal, store.execution
        self.interactions = BFFInteractions(self)
        self.lifecycle = None

    async def start_turn(self, conversation_id, request, **kwargs):
        """Return only after Journal, Turn, snapshot, grant and start command commit together."""
        accepted = await asyncio.to_thread(self._admit, conversation_id, request, **kwargs)
        if self.lifecycle:
            self.lifecycle.wake()
        return accepted

    def _admit(
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
        """Freeze one input and sending right that survives request cancellation."""
        conversation = self.repository.get_owned(conversation_id, tenant_id, subject_id)
        profile = self.releases.agents.resolve(
            conversation.agent_id, conversation.agent_profile_version
        )
        self.releases.require_root(profile)
        require_scopes(scopes, profile.required_scopes)
        evidence, expires_at = bounded_authorization(
            self.settings,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            evidence=authorization,
        )
        fingerprint = digest(
            {
                "conversation_id": conversation_id,
                "message": request.message,
                "agent_id": profile.agent_id,
                "agent_profile_version": profile.version,
            }
        )
        try:
            with self.store.sessions.begin() as session:
                turn, message, replay = self.repository.begin_turn(
                    conversation_id=conversation_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    message=request.message,
                    target_type="agent",
                    target_id=profile.agent_id,
                    target_version=profile.version,
                    session=session,
                )
                if replay:
                    row = self.store.lock(session, turn.run_id)
                    snapshot = session.get(RunExecutionRow, turn.run_id).snapshot
                else:
                    current = session.get(ConversationRow, conversation_id)
                    if (current.agent_id, current.agent_profile_version) != (
                        profile.agent_id,
                        profile.version,
                    ):
                        raise ExecutionConflict("conversation release changed during admission")
                    # Failed/cancelled checkpoints must not leak pending work into another Turn.
                    previous = session.scalar(
                        select(ConversationTurnRow)
                        .where(
                            ConversationTurnRow.conversation_id == conversation_id,
                            ConversationTurnRow.run_id != turn.run_id,
                        )
                        .order_by(ConversationTurnRow.created_at.desc())
                        .limit(1)
                    )
                    if previous and previous.status in {"failed", "cancelled"}:
                        current.agent_thread_id = str(uuid4())
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
                        profile, context, thread_id=current.agent_thread_id, input_hash=fingerprint
                    )
                    snapshot.update(
                        driver_version=DRIVER_VERSION,
                        backend_instance_id=self.store.backend_instance_id,
                        user_message_id=message.message_id,
                    )
                    self.execution.register(turn.run_id, snapshot, session=session)
                    row = RootRunRow(
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
                    self.store.authorization_event(
                        session, row, session.get(RunAuthorizationRow, turn.run_id), "admitted"
                    )
                    operation_id = "operation-" + digest([turn.run_id, "start"])
                    self.execution.prepare(
                        operation_id,
                        turn.run_id,
                        {
                            "kind": "start",
                            "payload": {
                                "input": {
                                    "messages": [
                                        {
                                            "role": "user",
                                            "content": request.message,
                                            "id": message.message_id,
                                        }
                                    ]
                                }
                            },
                            "scopes": sorted(scopes),
                        },
                        session=session,
                    )
                    self.store.command_inbox(session, row, operation_id)
                if notification_address is not None:
                    from financeclaw.shared.notifications.facts import bind_target

                    bind_target(
                        session,
                        row,
                        notification_address,
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        evidence=evidence,
                        replay=replay,
                    )
                if not replay:
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
        except JournalConflict as exc:
            raise IdempotencyConflict(str(exc)) from exc

    def root_for_task(self, run_id, *, tenant_id, subject_id):
        """Require exact root ownership and the BFF driver before any write or projection."""
        with self.store.sessions() as session:
            root = session.get(RootRunRow, run_id)
            execution = session.get(RunExecutionRow, run_id)
            if root is None or execution is None:
                raise RunNotFound("run not found")
            context = snapshot_context(execution.snapshot)
            if (context.tenant_id, context.subject_id) != (tenant_id, subject_id):
                raise RunNotFound("run not found")
            if (
                root.driver_version != DRIVER_VERSION
                or root.backend_instance_id != self.store.backend_instance_id
            ):
                raise ExecutionConflict("run protocol or backend binding does not match")
            return run_id

    async def status(self, run_id, *, tenant_id, subject_id, scopes=frozenset()):
        """Read a consistent progress/Journal snapshot; GET never drives the graph."""
        from financeclaw.bff.application.runs.progress import read_progress

        await asyncio.to_thread(
            self.root_for_task,
            run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        _, projection, content, _ = await asyncio.to_thread(
            read_progress, self.store.sessions, run_id, tenant_id, subject_id, None
        )
        if projection["status"] == "completed":
            projection = {
                **projection,
                "output": {"messages": [{"type": "assistant", "content": content}]},
            }
        return RunStatusResponse.model_validate(projection)

    def assert_owned(self, run_id, *, tenant_id, subject_id):
        """Verify ownership for HTTP and Channel callers without advancing a run."""
        self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def healthy(self):
        """BFF readiness depends on its own recovery loop."""
        return bool(self.lifecycle and await self.lifecycle.healthy())

    async def stream(
        self, run_id, *, tenant_id, subject_id, scopes=frozenset(), last_event_id=None
    ):
        """Replay durable progress and Journal; a disconnected subscriber cannot cancel work."""
        from financeclaw.bff.application.runs.progress import stream_progress

        await asyncio.to_thread(
            self.root_for_task,
            run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        async for event in stream_progress(
            self, run_id, tenant_id=tenant_id, subject_id=subject_id, last_event_id=last_event_id
        ):
            yield event

    def notifications(self, run_id, *, tenant_id, subject_id, revoke=False):
        """按根归属读取投递责任或显式撤销订阅，不暴露目的地址和正文。"""
        from financeclaw.shared.notifications.tables import (
            NotificationDeliveryRow,
            NotificationEventRow,
            NotificationTargetRow,
        )

        root_id = self.root_for_task(run_id, tenant_id=tenant_id, subject_id=subject_id)
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
                "card_sequence": target.card_sequence,
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

    async def assistant_content(self, run_id, *, tenant_id, subject_id):
        """最终结果从唯一 Journal 读取，最终文本属于当前顶层 Turn。"""
        root_id = await asyncio.to_thread(
            self.root_for_task,
            run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
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

    async def control_run(
        self,
        run_id,
        op,
        *,
        tenant_id,
        subject_id,
        scopes=(),
        authorization=None,
        command_id=None,
        expected_grant_revision=None,
    ):
        """共同的显式任务控制入口，消息重投不会重新计算授权期限。"""
        from financeclaw.bff.application.runs.controls import apply_control
        from financeclaw.shared.execution_ledger.receipts import read_receipt, save_receipt

        root_id = await asyncio.to_thread(
            self.root_for_task, run_id, tenant_id=tenant_id, subject_id=subject_id
        )
        fingerprint = digest([op, sorted(scopes), expected_grant_revision])

        def accept():
            """入账控制和回执，不在锁内调用异步后端。"""
            with self.store.sessions.begin() as session:
                root = self.store.lock(session, root_id)
                if command_id and read_receipt(session, root, command_id, fingerprint) is not None:
                    return
                apply_control(
                    self,
                    session,
                    root,
                    op,
                    scopes=scopes,
                    authorization=authorization,
                    revision=expected_grant_revision,
                )
                if command_id:
                    save_receipt(
                        session,
                        root,
                        command_id,
                        fingerprint,
                        {"status": root.projection["status"]},
                    )

        await asyncio.to_thread(accept)
        if self.lifecycle:
            self.lifecycle.wake()
        return await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def cancel(self, run_id, **kwargs):
        """持久请求停止本轮任务，确切停止由后端回执确认。"""
        return await self.control_run(run_id, "cancel", **kwargs)

    async def reauthorize(self, run_id, **kwargs):
        """显式恢复本轮的有限授权。"""
        return await self.control_run(run_id, "authorize", **kwargs)

    async def revoke_authorization(self, run_id, **kwargs):
        """撤销本轮后台授权并封闭后续受治理动作。"""
        return await self.control_run(run_id, "revoke", **kwargs)
