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

from langchain_core.messages import AIMessage

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
from financeclaw.orchestration.agents import AgentProfileCatalog

from .delegation_service import (
    DelegationService,
    delegation_projection,
    extract_handoff_interrupt,
)
from .ports import AgentServerClient
from .run_service import IdempotencyConflict, RunNotFound
from .streaming import (
    completed_stream_event,
    failed_stream_event,
    interrupted_stream_event,
    progress_stream_event,
    project_server_part,
)

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
        # 4. 首次执行（Turn 尚未绑定 server run）：创建 Agent 线程与执行上下文。
        if turn.server_run_id is None:
            await self.client.create_thread(conversation.agent_thread_id)
            context = ExecutionContext(
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
            )
            # 5. 重放路径先按 application_run_id 找回既有 server run，避免重复执行。
            server_run = (
                await self.client.find_run(
                    thread_id=conversation.agent_thread_id,
                    application_run_id=turn.run_id,
                )
                if replay
                else None
            )
            # 6. 找不到则以用户消息为输入创建新的 server run。
            if server_run is None:
                server_run = await self.client.create_run(
                    thread_id=conversation.agent_thread_id,
                    assistant_id=conversation.agent_id,
                    input={"messages": [{"role": "user", "content": request.message}]},
                    context=context.model_dump(mode="json"),
                    metadata=_server_metadata(
                        context,
                        stage="4",
                        conversation_id=conversation.conversation_id,
                        agent_profile_version=conversation.agent_profile_version,
                    ),
                )
            # 7. 把 server run 绑定回 Turn，落库映射与最新状态。
            turn = await asyncio.to_thread(
                self.repository.bind_server_run,
                turn.turn_id,
                server_run.run_id,
                server_run.status,
            )
        # 8. 返回受理结果。
        return RunAccepted(
            run_id=turn.run_id,
            thread_id=conversation.agent_thread_id,
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
        scopes: frozenset[str] = frozenset(),
        allow_dispatch: bool = True,
        allow_parent_resume: bool = True,
    ) -> RunStatusResponse:
        """轮询 Turn 对应 run 的最新状态，并在必要时推进 delegation 派发。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，用于 delegation 鉴权。
            allow_dispatch: 是否允许在 server 中断时派发 delegation。
            allow_parent_resume: 子运行到终态后是否允许恢复父运行。

        Returns:
            最新状态响应；delegation 场景附带子运行投影。

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
        # 1. 已完成的 Turn 直接短路返回。
        if turn.status.value == "completed":
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status="completed",
            )
        # 2. 存在未交付的 delegation：推进子运行并回传父状态。
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
        # 3. 尚未绑定 server run：维持当前业务状态。
        if turn.server_run_id is None:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status=turn.status.value,
            )
        # 4. 查询 server run 状态并按结果分派处理。
        server = await self.client.get_run(
            thread_id=conversation.agent_thread_id, run_id=turn.server_run_id
        )
        server_status = str(server.get("status", turn.status.value))
        output: Mapping[str, Any] | None = None
        if server_status in {"success", "completed"}:
            # 4a. 成功：取回最终输出，落库助手回复并补摘要。
            output = await self.client.join_run(
                thread_id=conversation.agent_thread_id, run_id=turn.server_run_id
            )
            final_content = _final_assistant_content(output)
            await asyncio.to_thread(
                self._record_completed,
                run_id,
                conversation.conversation_id,
                final_content,
            )
            status = "completed"
        elif server_status in {"error", "failed"}:
            # 4b. 失败：把 Turn 置为 failed。
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "failed")
            status = "failed"
        elif server_status == "interrupted":
            # 4c. 中断：typed handoff 且允许派发时启动 delegation；否则标记等待审批。
            handoff = extract_handoff_interrupt(server)
            if handoff is not None and allow_dispatch:
                if self.delegation_service is None:
                    raise RuntimeError("delegation service is not configured")
                delegation = await self.delegation_service.start(
                    handoff,
                    parent_run_id=run_id,
                    parent_turn_id=turn.turn_id,
                    conversation_id=conversation.conversation_id,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    scopes=scopes,
                )
                await asyncio.to_thread(
                    self.repository.update_turn_status,
                    run_id,
                    "waiting_child",
                )
                return await self._advance_delegation(
                    turn,
                    conversation,
                    delegation,
                    scopes=scopes,
                    allow_parent_resume=allow_parent_resume,
                )
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "interrupted")
            status = "interrupted"
        else:
            # 4d. 其余状态原样同步到 Turn。
            await asyncio.to_thread(self.repository.update_turn_status, run_id, server_status)
            status = server_status
        serializable_output = _jsonable_output(output)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=conversation.agent_thread_id,
            status=status,
            output=serializable_output,
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
        """提交顶层审批决定并恢复（或继续推进）对应 run。

        Args:
            run_id: 业务 run ID。
            decision: 审批决定（approve/reject/edit 及理由、参数 hash）。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，随执行上下文下发。

        Returns:
            恢复后的最新状态响应。

        Raises:
            RunNotFound: run 不存在或不属于当前主体。
            DelegationConflict: 子运行未处于等待审批状态。
            ApprovalExpired: 顶层审批窗口已超时。

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
        # 1. 存在未交付 delegation：把决定转发给子运行，再推进父 Turn。
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
            )
            if current.status is not DelegationStatus.INTERRUPTED:
                raise DelegationConflict("delegated child is not waiting for approval")
            current = await self.delegation_service.resume(
                current,
                decision,
                scopes=scopes,
            )
            return await self._advance_delegation(
                turn,
                conversation,
                current,
                scopes=scopes,
                allow_parent_resume=True,
            )
        # 2. 校验顶层审批窗口：超时则置 failed 并抛出。
        created_at = turn.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if self._clock() >= created_at + self.approval_timeout:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "failed")
            raise ApprovalExpired("approval window has expired; start a new memory proposal")
        # 3. 把决定映射为 server 端 resume 命令（EDIT 附带修改后的参数）。
        mapped: dict[str, Any] = {"type": decision.type.value}
        if decision.reason is not None:
            mapped["message"] = decision.reason
        if decision.arguments_hash is not None:
            mapped["arguments_hash"] = decision.arguments_hash
        if decision.type is ApprovalDecisionType.EDIT:
            mapped["arguments"] = decision.arguments
        # 4. 携带执行上下文恢复 server 运行。
        context = ExecutionContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            run_id=run_id,
        )
        result = await self.client.resume_run(
            thread_id=conversation.agent_thread_id,
            assistant_id=conversation.agent_id,
            command={"resume": {"decisions": [mapped]}},
            context=context.model_dump(mode="json"),
            metadata=_server_metadata(context, stage="4"),
        )
        # 5. 仍在等待审批则置 interrupted；完成则落最终回复并补摘要。
        status = "interrupted" if result.get("__interrupt__") else "completed"
        if status == "completed":
            final_content = _final_assistant_content(result)
            await asyncio.to_thread(
                self._record_completed,
                run_id,
                conversation.conversation_id,
                final_content,
            )
        else:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, status)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=conversation.agent_thread_id,
            status=status,
            output=_jsonable_output(result),
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
        if turn.server_run_id is not None and turn.status.value not in {"completed", "failed"}:
            try:
                async for part in self.client.stream_run(
                    thread_id=conversation.agent_thread_id,
                    run_id=turn.server_run_id,
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
            yield interrupted_stream_event(run_id)
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
            if turn.server_run_id is None:
                continue
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
        scopes: frozenset[str],
        allow_parent_resume: bool,
    ) -> RunStatusResponse:
        """推进 delegation：同步子运行状态，到终态时把结果交付回父运行。

        Args:
            turn: 父业务 Turn 记录。
            conversation: 父会话记录。
            record: 当前 delegation 记录。
            scopes: 调用方权限范围，随执行上下文下发。
            allow_parent_resume: 是否允许在子运行到终态后恢复父运行。

        Returns:
            父运行最新的状态响应。

        Raises:
            RuntimeError: delegation 服务未装配。

        """
        if self.delegation_service is None:
            raise RuntimeError("delegation service is not configured")
        # 1. 读取 delegation 最新状态。
        current = await self.delegation_service.status(
            record.delegation_id,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
        )
        terminal = {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
        }
        # 2. 子运行未到终态（或不允许父恢复）：父 Turn 置为 waiting_child/interrupted。
        if current.status not in terminal or not allow_parent_resume:
            parent_status = (
                "interrupted" if current.status is DelegationStatus.INTERRUPTED else "waiting_child"
            )
            await asyncio.to_thread(
                self.repository.update_turn_status,
                turn.run_id,
                parent_status,
            )
            return RunStatusResponse(
                run_id=turn.run_id,
                thread_id=conversation.agent_thread_id,
                status=parent_status,
                output={"delegation": delegation_projection(current)},
            )

        # 3. 子运行到终态：以 delegation 结果作为 resume 命令恢复父运行。
        context = ExecutionContext(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
        )
        result = await self.client.resume_run(
            thread_id=conversation.agent_thread_id,
            assistant_id=conversation.agent_id,
            command={"resume": self.delegation_service.result(current).model_dump(mode="json")},
            context=context.model_dump(mode="json"),
            metadata=_server_metadata(
                context,
                stage="4",
                delegation_id=current.delegation_id,
            ),
        )
        # 4. 标记 delegation 已交付父运行。
        await self.delegation_service.mark_delivered(current)
        # 5. 父运行又发出新的 handoff：递归派发下一个 delegation。
        next_handoff = extract_handoff_interrupt(result)
        if next_handoff is not None:
            next_record = await self.delegation_service.start(
                next_handoff,
                parent_run_id=turn.run_id,
                parent_turn_id=turn.turn_id,
                conversation_id=conversation.conversation_id,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
                scopes=scopes,
            )
            return await self._advance_delegation(
                turn,
                conversation,
                next_record,
                scopes=scopes,
                allow_parent_resume=allow_parent_resume,
            )
        # 6. 父运行完成：落最终回复并补摘要。
        final_content = _final_assistant_content(result)
        await asyncio.to_thread(
            self._record_completed,
            turn.run_id,
            conversation.conversation_id,
            final_content,
        )
        return RunStatusResponse(
            run_id=turn.run_id,
            thread_id=conversation.agent_thread_id,
            status="completed",
            output=_jsonable_output(result),
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


def _final_assistant_content(output: Mapping[str, Any]) -> str | None:
    """从运行输出的消息列表中取最后一条 AI/assistant 消息的文本内容。

    Args:
        output: 运行输出映射（含 "messages" 键时生效）。

    Returns:
        最终回复文本；非字符串内容序列化为 JSON，找不到时为 None。

    """
    messages = output.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return (
                message.content if isinstance(message.content, str) else json.dumps(message.content)
            )
        if isinstance(message, Mapping) and message.get("type") in {"ai", "assistant"}:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content, default=str)
    return None


def _jsonable_output(output: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """把输出映射转换为可 JSON 序列化的字典（Pydantic 模型逐个导出）。

    Args:
        output: 原始输出映射；None 原样返回。

    Returns:
        转换后的字典；入参为 None 时返回 None。

    """
    if output is None:
        return None
    converted: dict[str, Any] = {}
    for key, value in output.items():
        if isinstance(value, list):
            converted[key] = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in value
            ]
        elif hasattr(value, "model_dump"):
            converted[key] = value.model_dump(mode="json")
        else:
            converted[key] = value
    return converted
