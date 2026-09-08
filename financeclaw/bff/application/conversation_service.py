"""BFF 会话用例：创建/读取 Journal，并通过公开协调 API 提交运行操作。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from financeclaw.coordination.api import ConversationRunService
from financeclaw.kernel.agents import AgentProfileCatalog
from financeclaw.kernel.responses import (
    ApprovalDecision,
    ConversationMessageResponse,
    ConversationMessagesResponse,
    ConversationResponse,
    ConversationTurnRequest,
    RunAccepted,
    RunStatusResponse,
    StreamEvent,
)
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository


class ConversationService:
    """负责产品会话入口；执行推进与挂起恢复由注入的 Coordination 服务负责。"""

    ROOT_AGENT_ID = "finance_agent"

    def __init__(
        self,
        repository: SqlAlchemyConversationRepository,
        agent_profiles: AgentProfileCatalog,
        *,
        runs: ConversationRunService,
    ) -> None:
        """复用 Journal 与发布目录，并显式注入同库的运行协调服务。"""
        self.repository = repository
        self.agent_profiles = agent_profiles
        self.runs = runs

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
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return await self.runs.assistant_content(run_id, tenant_id=tenant_id, subject_id=subject_id)

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
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return await self.runs.start_turn(
            conversation_id,
            request,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            idempotency_key=idempotency_key,
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
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return await self.runs.status(
            run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            allow_dispatch=allow_dispatch,
            allow_parent_resume=allow_parent_resume,
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
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return await self.runs.resume(
            run_id, decision, tenant_id=tenant_id, subject_id=subject_id, scopes=scopes
        )

    async def cancel(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return await self.runs.cancel(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def stream(
        self,
        run_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str] = frozenset(),
    ) -> AsyncIterator[StreamEvent]:
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        async for event in self.runs.stream(
            run_id, tenant_id=tenant_id, subject_id=subject_id, scopes=scopes
        ):
            yield event

    def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return self.runs.assert_owned(run_id, tenant_id=tenant_id, subject_id=subject_id)

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        """通过 Coordination 的公开会话运行接口处理本次请求。"""
        return await self.runs.reconcile_incomplete()


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
