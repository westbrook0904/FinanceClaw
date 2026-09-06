"""会话应用服务：面向 BFF 编排会话创建、Turn 提交、状态轮询、审批恢复与流式订阅。

同时持久化业务 run/thread/server run 映射，并驱动 delegation 派发与会话摘要生成。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from financeclaw.kernel import (
    ApprovalDecision,
    ApprovalDecisionType,
    ConversationMessageResponse,
    ConversationMessagesResponse,
    ConversationResponse,
    ConversationTurnRequest,
    ExecutionContext,
    RunAccepted,
    RunStatusResponse,
    StreamEvent,
)
from financeclaw.modules.conversation import (
    ConversationNotFound,
    MessageRole,
    SqlAlchemyConversationRepository,
    SummaryService,
)
from financeclaw.modules.conversation import (
    IdempotencyConflict as JournalIdempotencyConflict,
)
from financeclaw.modules.delegation import DelegationConflict, DelegationRecord, DelegationStatus
from financeclaw.modules.execution import ExecutionConflict, snapshot_context
from financeclaw.modules.execution.repository import digest
from financeclaw.orchestration.agents import AgentProfileCatalog

from .delegation_service import (
    DelegationService,
    delegation_projection,
)
from .execution_service import ExecutionService, agent_snapshot, verify_agent_snapshot
from .interaction_service import InteractionService, waiting_reason
from .ports import AgentServerClient
from .run_observation import observe_run
from .run_service import IdempotencyConflict, RunNotFound
from .streaming import (
    completed_stream_event,
    failed_stream_event,
    interrupted_stream_event,
    progress_stream_event,
    project_server_part,
)
from .streaming import final_assistant_content as _final_assistant_content

LOGGER = logging.getLogger(__name__)


class ApprovalExpired(RuntimeError):
    """顶层 Agent 的审批窗口已超时，无法继续恢复运行时抛出。"""

    pass


class ConversationService:
    """会话用例服务：把 BFF 的会话操作翻译为仓储写入与 Agent Server 调用。

    使用场景：承载 POST /v1/conversations（create）、提交 message-only Turn
    （start_turn）、轮询 Run 状态并按需派发 delegation（status）、提交审批决定
    （resume）、订阅流式事件（stream），以及重启后的未完成 Turn 对账
    （reconcile_incomplete）。会话固定绑定顶层 Agent finance_agent。

    Attributes:
        ROOT_AGENT_ID: 会话默认绑定的顶层 Agent ID（"finance_agent"）。
        client: Agent Server 客户端 Port，负责 thread/run 的创建、查询与恢复。
        repository: 会话仓储，持久化会话、Turn、消息与 server run 绑定关系。
        agent_profiles: Agent Profile 目录，用于解析顶层 Agent 的版本信息。
        delegation_service: delegation 服务，处理 Workflow 与领域 Agent 派发；
            未启用派发能力时可为 None。
        summary_service: 会话摘要服务，Turn 完成后补齐分段与层级摘要；
            未启用摘要时可为 None。
        approval_timeout: 顶层审批窗口时长，超时后 resume 将拒绝恢复。

    """

    ROOT_AGENT_ID = "finance_agent"

    def __init__(
        self,
        client: AgentServerClient,
        repository: SqlAlchemyConversationRepository,
        agent_profiles: AgentProfileCatalog,
        *,
        delegation_service: DelegationService | None = None,
        summary_service: SummaryService | None = None,
        approval_timeout_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """装配会话服务依赖并校验配置。

        Args:
            client: Agent Server 客户端 Port。
            repository: 会话仓储实现。
            agent_profiles: Agent Profile 目录。
            delegation_service: 可选的 delegation 服务。
            summary_service: 可选的会话摘要服务。
            approval_timeout_seconds: 顶层审批窗口时长（秒），不可为负。
            clock: 可注入的时钟，便于测试审批超时；缺省取当前 UTC 时间。

        Raises:
            ValueError: approval_timeout_seconds 为负数。

        """
        if approval_timeout_seconds < 0:
            raise ValueError("approval timeout cannot be negative")
        self.client = client
        self.repository = repository
        self.agent_profiles = agent_profiles
        self.delegation_service = delegation_service
        self.summary_service = summary_service
        self.approval_timeout = timedelta(seconds=approval_timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.execution = repository.execution
        self.operations = ExecutionService(client, self.execution)
        self.interactions = InteractionService(
            client,
            self.execution,
            agent_profiles=agent_profiles,
            workflow_catalog=delegation_service.workflow_service.catalog
            if delegation_service
            else None,
            clock=lambda: self._clock(),
        )

    async def create(
        self,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> ConversationResponse:
        """为租户与主体创建绑定顶层 Agent 的新会话。

        Args:
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            新会话的 ID、状态与创建时间。

        """
        # 1. 解析顶层 Agent Profile 以固定其版本。
        profile = self.agent_profiles.resolve(self.ROOT_AGENT_ID)
        # 2. 在线程池中落库创建会话（避免阻塞事件循环）。
        conversation = await asyncio.to_thread(
            self.repository.create_conversation,
            tenant_id=tenant_id,
            subject_id=subject_id,
            agent_id=profile.agent_id,
            agent_profile_version=profile.version,
        )
        # 3. 投影为 API 响应。
        return _conversation_response(conversation)

    async def get_or_create_channel_conversation(
        self,
        *,
        channel: str,
        app_id: str,
        tenant_key: str,
        external_user_id: str,
        external_chat_id: str,
        tenant_id: str,
        subject_id: str,
    ) -> ConversationResponse:
        """原子解析或创建一个外部单聊绑定的顶层 Agent 会话。

        Args:
            channel: Channel 类型，一期固定为 ``feishu``。
            app_id: 飞书应用 ID。
            tenant_key: 已验证事件中的飞书租户键。
            external_user_id: 已验证事件中的发件人 open_id。
            external_chat_id: 已验证事件中的 P2P chat_id。
            tenant_id: 映射后的 FinanceClaw 租户 ID。
            subject_id: 映射后的 FinanceClaw 主体 ID。

        Returns:
            绑定对应的 Conversation 响应；首次访问时会连同绑定一起创建。

        """
        values = (
            channel,
            app_id,
            tenant_key,
            external_user_id,
            external_chat_id,
            tenant_id,
            subject_id,
        )
        if any(not value.strip() for value in values):
            raise ValueError("channel conversation identity fields cannot be empty")
        profile = self.agent_profiles.resolve(self.ROOT_AGENT_ID)
        _, conversation, _ = await asyncio.to_thread(
            self.repository.get_or_create_channel_conversation,
            channel=channel,
            app_id=app_id,
            tenant_key=tenant_key,
            external_user_id=external_user_id,
            external_chat_id=external_chat_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            agent_id=profile.agent_id,
            agent_profile_version=profile.version,
        )
        return _conversation_response(conversation)

    def get(self, conversation_id: str, *, tenant_id: str, subject_id: str) -> ConversationResponse:
        """查询归属于当前租户与主体的会话快照。

        Args:
            conversation_id: 会话 ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            会话的 ID、状态与创建时间。

        Raises:
            ConversationNotFound: 会话不存在或归属不匹配。

        """
        conversation = self.repository.get_owned(conversation_id, tenant_id, subject_id)
        return _conversation_response(conversation)

    def messages(
        self, conversation_id: str, *, tenant_id: str, subject_id: str
    ) -> ConversationMessagesResponse:
        """列出归属会话内的全部消息（按会话内顺序）。

        Args:
            conversation_id: 会话 ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            会话 ID 与消息列表（含消息 ID、Turn、序号与角色）。

        Raises:
            ConversationNotFound: 会话不存在或归属不匹配。

        """
        self.repository.get_owned(conversation_id, tenant_id, subject_id)
        messages = self.repository.list_messages(conversation_id)
        return ConversationMessagesResponse(
            conversation_id=conversation_id,
            messages=tuple(
                ConversationMessageResponse(
                    message_id=item.message_id,
                    turn_id=item.turn_id,
                    sequence=item.sequence,
                    parent_message_id=item.parent_message_id,
                    role=item.role.value,
                    content=item.content,
                    created_at=item.created_at.isoformat(),
                )
                for item in messages
            ),
        )

    async def assistant_content(
        self,
        run_id: str,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str | None:
        """读取指定会话 run 已落库的最终助手文本。

        Args:
            run_id: FinanceClaw 业务运行 ID。
            tenant_id: 归属租户 ID。
            subject_id: 归属主体 ID。

        Returns:
            Journal 中该 Turn 的最终助手文本；尚未写入时返回 ``None``。

        """
        try:
            turn, _ = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        messages = await asyncio.to_thread(self.repository.list_messages, turn.conversation_id)
        for message in reversed(messages):
            if message.turn_id == turn.turn_id and message.role is MessageRole.ASSISTANT:
                return message.content
        return None

    async def start_turn(
        self,
        conversation_id: str,
        request: ConversationTurnRequest,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
    ) -> RunAccepted:
        """提交一条用户消息并启动（或幂等重放）对应的服务端运行。

        Args:
            conversation_id: 目标会话 ID。
            request: 仅含 message 的 Turn 请求（message-only）。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，随执行上下文下发。
            idempotency_key: 客户端幂等键，重复提交需保持一致。

        Returns:
            受理结果：业务 run/Turn 标识、服务端 thread 与是否幂等重放。

        Raises:
            IdempotencyConflict: 同一幂等键被用于不同请求内容。

        """
        # 1. 校验会话归属并读取会话快照。
        conversation = await asyncio.to_thread(
            self.repository.get_owned,
            conversation_id,
            tenant_id,
            subject_id,
        )
        # 2. 计算请求指纹：绑定会话、消息与 Agent 版本，用于幂等冲突判定。
        try:
            profile = self.agent_profiles.resolve(
                conversation.agent_id, conversation.agent_profile_version
            )
        except LookupError as exc:
            raise ExecutionConflict(
                "conversation release is no longer deployed; create a new conversation"
            ) from exc
        request_hash = sha256(
            json.dumps(
                {
                    "conversation_id": conversation.conversation_id,
                    "message": request.message,
                    "agent_id": conversation.agent_id,
                    "agent_profile_version": conversation.agent_profile_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        # 3. 开启 Turn：仓储按幂等键判重，重复请求返回既有 Turn 并标记重放。
        try:
            turn, _, replay = await asyncio.to_thread(
                self.repository.begin_turn,
                conversation_id=conversation.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                message=request.message,
                target_type="agent",
                target_id=conversation.agent_id,
                target_version=conversation.agent_profile_version,
            )
        except JournalIdempotencyConflict as exc:
            raise IdempotencyConflict(str(exc)) from exc
        # 首次远程提交前冻结授权；旧 Turn 缺快照不得借幂等重放补成新权限。
        if not replay:
            context = ExecutionContext(
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                root_run_id=turn.run_id,
                request_clock=self._clock().isoformat(),
                data_classification=profile.data_classification,
            )
            await asyncio.to_thread(
                self.execution.register,
                turn.run_id,
                agent_snapshot(
                    profile,
                    context,
                    thread_id=conversation.agent_thread_id,
                    input_hash=request_hash,
                ),
            )
        execution = await asyncio.to_thread(self.execution.get, turn.run_id)
        verify_agent_snapshot(profile, execution["snapshot"])
        context = snapshot_context(execution["snapshot"], scopes)
        if turn.server_run_id is None:
            await self.client.create_thread(execution["snapshot"]["thread_id"])
            server_run = await self.operations.submit(
                turn.run_id,
                "start",
                thread_id=execution["snapshot"]["thread_id"],
                assistant_id=execution["snapshot"]["assistant_id"],
                input={"messages": [{"role": "user", "content": request.message}]},
                context=context.model_dump(mode="json"),
                metadata=_server_metadata(context, stage="6fix"),
            )
            if server_run is not None:
                turn = await asyncio.to_thread(
                    self.repository.bind_server_run,
                    turn.turn_id,
                    server_run.run_id,
                    server_run.status,
                )
        # 8. 返回受理结果。
        return RunAccepted(
            run_id=turn.run_id,
            thread_id=execution["snapshot"]["thread_id"],
            status=turn.status.value,
            target_kind="agent",
            idempotent_replay=replay,
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
        )

    async def status(
        self,
        run_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str] | None = None,
        allow_dispatch: bool = True,
        allow_parent_resume: bool = True,
    ) -> RunStatusResponse:
        """按精确 Server Run 观察；等待、未知提交与中断都不能误报完成。"""
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        if turn.status.value in {"completed", "failed", "cancelled"}:
            try:
                saved = await asyncio.to_thread(self.execution.get, run_id)
                thread_id = saved["snapshot"]["thread_id"]
            except ExecutionConflict:
                thread_id = conversation.agent_thread_id  # 已完成的旧 Journal 仍允许只读查询。
            content = await self.assistant_content(
                run_id, tenant_id=tenant_id, subject_id=subject_id
            )
            return RunStatusResponse(
                run_id=run_id,
                thread_id=thread_id,
                status=turn.status.value,
                output={"messages": [{"type": "assistant", "content": content}]}
                if content
                else None,
            )
        await self.interactions.reconcile_owner(run_id, scopes=scopes)
        uncertain = await self.operations.reconcile(run_id)
        execution = await asyncio.to_thread(self.execution.get, run_id)
        if execution["cancellation_requested"]:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status="cancellation_requested",
                waiting_reason="execution_stop_not_confirmed",
            )
        if uncertain:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status=turn.status.value,
                waiting_reason="submission_uncertain",
            )
        active = (
            await self.delegation_service.latest_for_parent(
                run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            if self.delegation_service is not None
            else None
        )
        if active is not None:
            return await self._advance_delegation(
                turn,
                conversation,
                active,
                scopes=scopes,
                allow_parent_resume=allow_parent_resume,
            )
        server_id = execution["server_run_id"]
        if server_id is None:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status=turn.status.value,
                waiting_reason="submission_uncertain",
            )
        server = await self.client.get_run(
            thread_id=execution["snapshot"]["thread_id"], run_id=server_id
        )
        if observe_run(server).kind == "completed":
            server = await self.client.join_run(
                thread_id=execution["snapshot"]["thread_id"],
                run_id=server_id,
            )
        return await self._observe_parent(
            turn,
            conversation,
            server,
            server_id=server_id,
            scopes=scopes,
            allow_dispatch=allow_dispatch,
            allow_parent_resume=allow_parent_resume,
        )

    async def _observe_parent(
        self,
        turn: Any,
        conversation: Any,
        result: Mapping[str, Any],
        *,
        server_id: str,
        scopes: frozenset[str] | None,
        allow_dispatch: bool = True,
        allow_parent_resume: bool = True,
    ) -> RunStatusResponse:
        """启动、轮询和所有恢复路径使用同一分类，不依据流结束或缺少 handoff 判终态。"""
        observed = observe_run(result)
        if observed.kind == "completed":
            await asyncio.to_thread(
                self.execution.set_waiting, turn.run_id, None, server_run_id=server_id
            )
            await asyncio.to_thread(
                self._record_completed,
                turn.run_id,
                conversation.conversation_id,
                _final_assistant_content(result),
            )
            return RunStatusResponse(
                run_id=turn.run_id,
                thread_id=conversation.agent_thread_id,
                status="completed",
                output=_public_output(result),
            )
        if observed.kind == "failed":
            await asyncio.to_thread(self.repository.update_turn_status, turn.run_id, "failed")
            return RunStatusResponse(
                run_id=turn.run_id, thread_id=conversation.agent_thread_id, status="failed"
            )
        if observed.kind == "running":
            await asyncio.to_thread(self.repository.update_turn_status, turn.run_id, "running")
            return RunStatusResponse(
                run_id=turn.run_id, thread_id=conversation.agent_thread_id, status="running"
            )
        payload = observed.payload or {}
        waiting = {
            "key": observed.interrupt_id or digest([server_id, payload]),
            "interrupt_id": observed.interrupt_id,
            "server_run_id": server_id,
            "kind": observed.kind,
            "payload": payload,
            "payload_hash": digest(payload),
            "expires_at": (self._clock() + self.approval_timeout).isoformat(),
        }
        waiting = await asyncio.to_thread(
            self.execution.set_waiting, turn.run_id, waiting, server_run_id=server_id
        )
        if observed.kind == "handoff" and allow_dispatch:
            if self.delegation_service is None:
                raise ExecutionConflict("delegation service is not configured")
            execution = await asyncio.to_thread(self.execution.get, turn.run_id)
            profile = self.agent_profiles.resolve(
                conversation.agent_id, conversation.agent_profile_version
            )
            verify_agent_snapshot(profile, execution["snapshot"])
            context = snapshot_context(execution["snapshot"], scopes)
            record = await self.delegation_service.start(
                observed.handoff,
                parent_run_id=turn.run_id,
                parent_turn_id=turn.turn_id,
                conversation_id=conversation.conversation_id,
                tenant_id=turn.tenant_id,
                subject_id=turn.subject_id,
                scopes=context.scopes,
                parent_snapshot=execution["snapshot"],
                parent_server_run_id=server_id,
                parent_interrupt_id=observed.interrupt_id,
            )
            return await self._advance_delegation(
                turn,
                conversation,
                record,
                scopes=scopes,
                allow_parent_resume=allow_parent_resume,
            )
        if observed.kind in {"hitl", "interaction"} and observed.interrupt_id:
            interaction = await self.interactions.observe_agent(
                turn.run_id,
                observed,
                server_run_id=server_id,
                expires_at=datetime.fromisoformat(waiting["expires_at"]),
                checkpoint_id=(result.get("checkpoint") or {}).get("checkpoint_id"),
            )
            public = await self.interactions.public(interaction)
            await asyncio.to_thread(self.repository.update_turn_status, turn.run_id, "interrupted")
            return RunStatusResponse(
                run_id=turn.run_id,
                thread_id=conversation.agent_thread_id,
                status="interrupted",
                waiting_reason=waiting_reason(public),
                pending_interactions=(public,),
            )
        await asyncio.to_thread(self.repository.update_turn_status, turn.run_id, "interrupted")
        reason = "approval_required" if observed.kind == "hitl" else "unsupported_interruption"
        projection = ()
        if observed.kind == "hitl":
            from financeclaw.orchestration.agents.middleware import redact_sensitive

            action = payload["action_requests"][0]
            reason = (
                "approval_expired"
                if self._clock() >= datetime.fromisoformat(waiting["expires_at"])
                else "approval_required"
            )
            projection = (
                {
                    "interrupt_id": waiting["key"],
                    "kind": "approval",
                    "action": redact_sensitive(action),
                    "arguments_hash": digest(action),
                    "allowed_decisions": ["approve", "reject"],
                    "expires_at": waiting["expires_at"],
                },
            )
        return RunStatusResponse(
            run_id=turn.run_id,
            thread_id=conversation.agent_thread_id,
            status="interrupted",
            waiting_reason=reason,
            pending_interactions=projection,
        )

    async def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
    ) -> RunStatusResponse:
        """只恢复一个已登记且未变化的审批位置，执行权限与审批权限分开校验。"""
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        execution = await asyncio.to_thread(self.execution.get, run_id)
        if execution["cancellation_requested"]:
            raise ExecutionConflict("cancellation requested; no further resume is allowed")
        active = (
            await self.delegation_service.latest_for_parent(
                run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            if self.delegation_service is not None
            else None
        )
        if active is not None:
            current = await self.delegation_service.status(
                active.delegation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
            )
            if current.status is not DelegationStatus.INTERRUPTED:
                raise DelegationConflict("delegated child is not waiting for approval")
            current = await self.delegation_service.resume(current, decision, scopes=scopes)
            return await self._advance_delegation(
                turn, conversation, current, scopes=scopes, allow_parent_resume=True
            )
        if await self.interactions.resume_legacy(
            run_id, decision, tenant_id=tenant_id, subject_id=subject_id, scopes=scopes
        ):
            return await self.status(
                run_id, tenant_id=tenant_id, subject_id=subject_id, scopes=scopes
            )
        waiting = execution["waiting"]
        if not waiting or waiting["kind"] != "hitl":
            raise ExecutionConflict("run has no single supported pending approval")
        if self._clock() >= datetime.fromisoformat(waiting["expires_at"]):
            raise ApprovalExpired(
                "approval window has expired; cancel or start an independent conversation"
            )
        if decision.type is ApprovalDecisionType.EDIT:
            raise ExecutionConflict("edited actions require a new snapshot and approval")
        action = waiting["payload"]["action_requests"][0]
        if decision.interrupt_id != waiting["key"] and (
            waiting["interrupt_id"] is not None or decision.interrupt_id is not None
        ):
            raise ExecutionConflict("approval refers to another interrupt instance")
        if decision.arguments_hash != digest(action):
            raise ExecutionConflict("approval hash does not match the pending action")
        profile = self.agent_profiles.resolve(
            conversation.agent_id, conversation.agent_profile_version
        )
        verify_agent_snapshot(profile, execution["snapshot"])
        context = snapshot_context(execution["snapshot"], scopes)
        # 审批不赋予执行权限。实际 Tool 仍会在执行前按冻结版本再次治理。
        if not context.scopes:
            raise ExecutionConflict("current authorization does not permit resuming this action")
        mapped = {"type": decision.type.value}
        if decision.reason is not None:
            mapped["message"] = decision.reason
        key = "approval:" + waiting["key"]
        server_run = await self.operations.submit(
            run_id,
            key,
            thread_id=execution["snapshot"]["thread_id"],
            assistant_id=execution["snapshot"]["assistant_id"],
            command=self._resume_command(waiting["interrupt_id"], {"decisions": [mapped]}),
            context=context.model_dump(mode="json"),
            metadata=_server_metadata(context, stage="6fix"),
            predecessor=waiting["server_run_id"],
        )
        result = await self.operations.result(run_id, key) if server_run is not None else None
        if result is None:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status="running" if server_run else "interrupted",
                waiting_reason="resume_pending" if server_run else "submission_uncertain",
            )
        return await self._observe_parent(
            turn, conversation, result, server_id=server_run.run_id, scopes=scopes
        )

    @staticmethod
    def _resume_command(interrupt_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        """优先按原生 interrupt ID 恢复；无 ID 的旧单中断只保留唯一目标兼容路径。"""
        return {"resume": {interrupt_id: payload} if interrupt_id else payload}

    async def cancel(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        """先封闭派发，再逐一确认子树停止；不会把本地取消当作副作用回滚。"""
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        if turn.status.value in {"completed", "failed", "cancelled"}:
            return RunStatusResponse(
                run_id=run_id, thread_id=conversation.agent_thread_id, status=turn.status.value
            )
        await asyncio.to_thread(self.interactions.repository.cancel_tree, run_id, now=self._clock())
        await asyncio.to_thread(
            self.repository.update_turn_status, run_id, "cancellation_requested"
        )
        confirmed = await self.operations.confirm_tree_stopped(run_id)
        if confirmed:
            await asyncio.to_thread(self.repository.confirm_cancel, run_id)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=conversation.agent_thread_id,
            status="cancelled" if confirmed else "cancellation_requested",
            waiting_reason=None if confirmed else "execution_stop_not_confirmed",
        )

    async def stream(
        self,
        run_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str] = frozenset(),
    ) -> AsyncIterator[StreamEvent]:
        """订阅指定会话 server run，并以 Journal 校正最终助手文本。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，用于流结束后的 delegation 推进。

        Yields:
            归一化后的流式事件（事件名 + 数据载荷）。

        Raises:
            RunNotFound: run 不存在或不属于当前主体。

        """
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        execution = await asyncio.to_thread(self.execution.get, run_id)
        server_run_id = execution["server_run_id"]
        if server_run_id is not None and turn.status.value not in {
            "completed",
            "failed",
            "cancelled",
            "cancellation_requested",
        }:
            try:
                async for part in self.client.stream_run(
                    thread_id=execution["snapshot"]["thread_id"],
                    run_id=server_run_id,
                ):
                    projected = project_server_part(part)
                    if projected is not None:
                        yield projected
            except Exception:
                LOGGER.warning(
                    "conversation run stream ended unexpectedly",
                    extra={"run_id": run_id},
                )

        try:
            final = await self.status(
                run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
            )
        except Exception:
            LOGGER.warning("conversation final reconciliation failed", extra={"run_id": run_id})
            yield failed_stream_event(run_id)
            return
        if final.status == "completed":
            content = await self.assistant_content(
                run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            output = final.output or {}
            if content is not None:
                output = {**output, "messages": [{"type": "assistant", "content": content}]}
            yield completed_stream_event(run_id, output)
        elif final.status == "interrupted":
            yield interrupted_stream_event(
                run_id,
                waiting_reason=final.waiting_reason,
                pending_interactions=final.pending_interactions,
            )
        elif final.status == "failed":
            yield failed_stream_event(run_id)
        else:
            yield progress_stream_event(run_id, final.status)

    def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        """校验 run 归属于当前租户与主体，不通过则抛出 RunNotFound。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        """
        try:
            turn = self.repository.get_turn_owned(run_id, tenant_id, subject_id)
            self.repository.get_owned(turn.conversation_id, tenant_id, subject_id)
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        """对账所有未完成 Turn：刷新状态但不触发新的派发或父恢复。

        Returns:
            本次完成对账的业务 run ID 列表。

        """
        reconciled: list[str] = []
        # 1. 拉取所有未完成 Turn。
        turns = await asyncio.to_thread(self.repository.list_incomplete_turns)
        for turn in turns:
            # 2. 按归属刷新状态；禁用派发与父恢复，避免对账产生新副作用。
            await self.status(
                turn.run_id,
                tenant_id=turn.tenant_id,
                subject_id=turn.subject_id,
                allow_dispatch=False,
                allow_parent_resume=False,
            )
            reconciled.append(turn.run_id)
        # 3. 返回已对账的 run ID 列表。
        return tuple(reconciled)

    async def _advance_delegation(
        self,
        turn: Any,
        conversation: Any,
        record: DelegationRecord,
        *,
        scopes: frozenset[str] | None,
        allow_parent_resume: bool,
    ) -> RunStatusResponse:
        """子终态与父结果交付分离；并发查询共享同一个持久化恢复操作。"""
        if self.delegation_service is None:
            raise RuntimeError("delegation service is not configured")
        execution = await asyncio.to_thread(self.execution.get, turn.run_id)
        if execution["cancellation_requested"]:
            return RunStatusResponse(
                run_id=turn.run_id,
                thread_id=conversation.agent_thread_id,
                status="cancellation_requested",
                waiting_reason="execution_stop_not_confirmed",
            )
        current = await self.delegation_service.status(
            record.delegation_id,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
        )
        terminal = {DelegationStatus.COMPLETED, DelegationStatus.REJECTED, DelegationStatus.FAILED}
        if current.status is DelegationStatus.DELIVERED:
            # 另一请求已完成原子交付；刷新当前 Server Run，不能回退 waiting_child。
            return await self.status(
                turn.run_id, tenant_id=turn.tenant_id, subject_id=turn.subject_id, scopes=scopes
            )
        if current.status not in terminal or not allow_parent_resume:
            status = (
                "interrupted" if current.status is DelegationStatus.INTERRUPTED else "waiting_child"
            )
            await asyncio.to_thread(self.repository.update_turn_status, turn.run_id, status)
            reason = (
                "unsupported_child_interaction"
                if current.kind.value == "agent" and status == "interrupted"
                else "child_approval_required"
                if status == "interrupted"
                else "waiting_child"
            )
            interactions = ()
            if status == "interrupted" and current.output_payload:
                reason = current.output_payload.get("waiting_reason") or reason
                interactions = tuple(current.output_payload.get("pending_interactions", ()))
            return RunStatusResponse(
                run_id=turn.run_id,
                thread_id=conversation.agent_thread_id,
                status=status,
                waiting_reason=reason,
                pending_interactions=interactions,
                output={"delegation": delegation_projection(current)},
            )
        profile = self.agent_profiles.resolve(
            conversation.agent_id, conversation.agent_profile_version
        )
        verify_agent_snapshot(profile, execution["snapshot"])
        context = snapshot_context(execution["snapshot"], scopes)
        if not current.execution_snapshot:
            raise ExecutionConflict("delegation execution snapshot is missing")
        if current.status is DelegationStatus.REJECTED:
            await asyncio.to_thread(self.execution.deny_side_effects, turn.run_id)
        key = "delivery:" + current.delegation_id
        server_run = await self.operations.submit(
            turn.run_id,
            key,
            thread_id=execution["snapshot"]["thread_id"],
            assistant_id=execution["snapshot"]["assistant_id"],
            command=self._resume_command(
                current.execution_snapshot.get("parent_interrupt_id"),
                self.delegation_service.result(current).model_dump(mode="json"),
            ),
            context=context.model_dump(mode="json"),
            metadata=_server_metadata(context, stage="6fix", delegation_id=current.delegation_id),
            predecessor=current.execution_snapshot.get("parent_server_run_id"),
        )
        result = (
            await self.operations.result(
                turn.run_id,
                key,
                delegation_id=current.delegation_id,
                audit=self.delegation_service.audit,
            )
            if server_run is not None
            else None
        )
        if result is None:
            return RunStatusResponse(
                run_id=turn.run_id,
                thread_id=conversation.agent_thread_id,
                status="waiting_child",
                waiting_reason="delivery_pending" if server_run else "submission_uncertain",
                output={"delegation": delegation_projection(current)},
            )
        return await self._observe_parent(
            turn,
            conversation,
            result,
            server_id=server_run.run_id,
            scopes=scopes,
            allow_parent_resume=allow_parent_resume,
        )

    def _owned_turn_and_conversation(
        self, run_id: str, tenant_id: str, subject_id: str
    ) -> tuple[Any, Any]:
        """加载归属于当前租户与主体的 Turn 及其会话。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            （Turn 记录, 会话记录）二元组。

        """
        turn = self.repository.get_turn_owned(run_id, tenant_id, subject_id)
        conversation = self.repository.get_owned(turn.conversation_id, tenant_id, subject_id)
        return turn, conversation

    def _record_completed(
        self,
        run_id: str,
        conversation_id: str,
        final_content: str | None,
    ) -> None:
        """落库 run 完成结果：追加助手回复、置 Turn 完成并补齐摘要。

        Args:
            run_id: 业务 run ID。
            conversation_id: 会话 ID。
            final_content: 最终助手回复文本；None 时跳过消息写入。

        """
        # 1. 有最终回复时追加 assistant 消息。
        if final_content is not None:
            self.repository.append_assistant_message(run_id=run_id, content=final_content)
        # 2. 置 Turn 为完成态。
        self.repository.update_turn_status(run_id, "completed")
        # 3. 补齐会话摘要的分段与层级。
        if self.summary_service is not None:
            self.summary_service.build_missing_segments(conversation_id)
            self.summary_service.build_hierarchy(conversation_id)


def _conversation_response(conversation: Any) -> ConversationResponse:
    """把会话记录投影为 API 响应（ID、状态与创建时间）。

    Args:
        conversation: 会话记录。

    Returns:
        会话响应对象。

    """
    return ConversationResponse(
        conversation_id=conversation.conversation_id,
        status=conversation.status.value,
        created_at=conversation.created_at.isoformat(),
    )


def _server_metadata(context: ExecutionContext, **extra: str) -> dict[str, str]:
    """构建写入 server run 的追踪元数据。

    Args:
        context: 执行上下文，提供租户/主体/turn/run 的追踪字段。
        **extra: 追加的元数据键值（如 stage、conversation_id）。

    Returns:
        以 application_run_id 承载业务 run 映射的元数据字典。

    """
    metadata = context.trace_metadata()
    metadata["application_run_id"] = metadata.pop("run_id")
    metadata.update(extra)
    return metadata


def _public_output(output: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """公开结果仅含最终助手文本，不返回 checkpoint、工具往返、Prompt 或推理块。"""
    if output is None:
        return None
    content = _final_assistant_content(output)
    return {"messages": [{"type": "assistant", "content": content}]} if content else {}
