"""围绕 Agent Server 运行编排持久化的多轮会话。"""

import asyncio
import json
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


class ApprovalExpired(RuntimeError):
    """表示审批窗口已过期，原检查点仍保留但不得继续恢复。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """


class ConversationService:
    """协调根 Agent 会话的 Journal、Agent Server 运行、审批和子委派。

    适用场景：
        用于应用用例需要跨仓储、外部端口或领域策略协调一致结果的场景。

    属性：
        ROOT_AGENT_ID: 表示 `root_agent_id` 这一受限枚举值。
        client: 负责与外部 Agent Server 或供应商通信的端口实现。
        repository: 负责领域状态读写和事务一致性的仓储。
        agent_profiles: 可按稳定标识和版本解析 Agent 配置的只读目录。
        delegation_service: 负责父运行与子目标之间状态协调的应用服务。
        summary_service: 负责构建和维护分层会话摘要的领域服务。
        approval_timeout: 人工审批允许等待的时长；超时后禁止恢复原检查点。
        _clock: 可替换时间源，便于统一 UTC 时间并支持确定性测试。
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
        """注入并保存会话Service所需的协作对象，同时校验构造期不变量。"""
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
        """创建归属指定租户和主体的根会话，并固定当前根 Agent 配置版本。"""
        profile = self.agent_profiles.resolve(self.ROOT_AGENT_ID)
        conversation = await asyncio.to_thread(
            self.repository.create_conversation,
            tenant_id=tenant_id,
            subject_id=subject_id,
            agent_id=profile.agent_id,
            agent_profile_version=profile.version,
        )
        return _conversation_response(conversation)

    def get(self, conversation_id: str, *, tenant_id: str, subject_id: str) -> ConversationResponse:
        """读取调用主体拥有的会话，并转换为不泄露内部模型的响应。"""
        conversation = self.repository.get_owned(conversation_id, tenant_id, subject_id)
        return _conversation_response(conversation)

    def messages(
        self, conversation_id: str, *, tenant_id: str, subject_id: str
    ) -> ConversationMessagesResponse:
        """校验会话所有权后，按稳定序号返回该会话的可见消息。"""
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
        """以幂等方式追加用户消息、创建会话轮次，并确保对应服务端运行已绑定。"""
        conversation = await asyncio.to_thread(
            self.repository.get_owned,
            conversation_id,
            tenant_id,
            subject_id,
        )
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
            server_run = (
                await self.client.find_run(
                    thread_id=conversation.agent_thread_id,
                    application_run_id=turn.run_id,
                )
                if replay
                else None
            )
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
            turn = await asyncio.to_thread(
                self.repository.bind_server_run,
                turn.turn_id,
                server_run.run_id,
                server_run.status,
            )
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
        """汇总本地轮次、活动委派与服务端运行状态，并把可确认的终态写回 Journal。"""
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        if turn.status.value == "completed":
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status="completed",
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
        if turn.server_run_id is None:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status=turn.status.value,
            )
        server = await self.client.get_run(
            thread_id=conversation.agent_thread_id, run_id=turn.server_run_id
        )
        server_status = str(server.get("status", turn.status.value))
        output: Mapping[str, Any] | None = None
        if server_status in {"success", "completed"}:
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
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "failed")
            status = "failed"
        elif server_status == "interrupted":
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
        """校验审批时效与参数绑定后，恢复当前委派或根 Agent 检查点。"""
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
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
        created_at = turn.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if self._clock() >= created_at + self.approval_timeout:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "failed")
            raise ApprovalExpired("approval window has expired; start a new memory proposal")
        mapped: dict[str, Any] = {"type": decision.type.value}
        if decision.reason is not None:
            mapped["message"] = decision.reason
        if decision.arguments_hash is not None:
            mapped["arguments_hash"] = decision.arguments_hash
        if decision.type is ApprovalDecisionType.EDIT:
            mapped["arguments"] = decision.arguments
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
        self, run_id: str, *, tenant_id: str, subject_id: str
    ) -> AsyncIterator[StreamEvent]:
        """校验运行所有权后，将服务端线程事件转换为统一流事件。"""
        try:
            _, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        async for part in self.client.stream_thread(
            thread_id=conversation.agent_thread_id, assistant_id=conversation.agent_id
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

    def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        """验证会话Service满足当前边界要求，否则抛出明确异常。"""
        try:
            turn = self.repository.get_turn_owned(run_id, tenant_id, subject_id)
            self.repository.get_owned(turn.conversation_id, tenant_id, subject_id)
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        """扫描非终态轮次并逐一拉取远端状态，供启动恢复或定时修复使用。"""
        reconciled: list[str] = []
        turns = await asyncio.to_thread(self.repository.list_incomplete_turns)
        for turn in turns:
            if turn.server_run_id is None:
                continue
            await self.status(
                turn.run_id,
                tenant_id=turn.tenant_id,
                subject_id=turn.subject_id,
                allow_dispatch=False,
                allow_parent_resume=False,
            )
            reconciled.append(turn.run_id)
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
        """推进活动子委派；完成后向父检查点交付结果并继续根运行。"""
        if self.delegation_service is None:
            raise RuntimeError("delegation service is not configured")
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
        await self.delegation_service.mark_delivered(current)
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
        """读取记录并同时校验租户与主体所有权，避免越权访问。"""
        turn = self.repository.get_turn_owned(run_id, tenant_id, subject_id)
        conversation = self.repository.get_owned(turn.conversation_id, tenant_id, subject_id)
        return turn, conversation

    def _record_completed(
        self,
        run_id: str,
        conversation_id: str,
        final_content: str | None,
    ) -> None:
        """把已确认的会话Service事实持久化。"""
        if final_content is not None:
            self.repository.append_assistant_message(run_id=run_id, content=final_content)
        self.repository.update_turn_status(run_id, "completed")
        if self.summary_service is not None:
            self.summary_service.build_missing_segments(conversation_id)
            self.summary_service.build_hierarchy(conversation_id)


def _conversation_response(conversation: Any) -> ConversationResponse:
    """把内部会话记录转换为公开会话响应。"""
    return ConversationResponse(
        conversation_id=conversation.conversation_id,
        status=conversation.status.value,
        created_at=conversation.created_at.isoformat(),
    )


def _server_metadata(context: ExecutionContext, **extra: str) -> dict[str, str]:
    """组合执行上下文与阶段字段，生成不含敏感原值的服务端元数据。"""
    metadata = context.trace_metadata()
    metadata["application_run_id"] = metadata.pop("run_id")
    metadata.update(extra)
    return metadata


def _final_assistant_content(output: Mapping[str, Any]) -> str | None:
    """从服务端输出消息中提取最后一条助手文本。"""
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
    """把映射输出递归转换为可安全进入响应模型的 JSON 结构。"""
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
