"""会话日志的持久化仓库：负责领域模型与 ORM 表的互转及事务落库。

提供会话、turn、消息、摘要与 Manifest 的幂等写入与归属查询能力，
是 Conversation Journal 唯一的数据库访问层。
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Conversation,
    ConversationMessage,
    ConversationStatus,
    ConversationSummary,
    ConversationTurn,
    MessageRole,
    ModelContextManifest,
    SummaryStatus,
    TurnStatus,
)
from .tables import (
    ConversationMessageRow,
    ConversationRow,
    ConversationSummaryRow,
    ConversationTurnRow,
    ModelContextManifestRow,
)


class ConversationNotFound(LookupError):
    """按归属查询的会话资源不存在时抛出的异常。

    使用场景：get_owned、get_turn_owned 等方法在目标记录不存在时抛出，
    BFF 层通常据此返回 404。
    """

    pass


class ConversationConflict(RuntimeError):
    """会话状态或内容与操作前提冲突时抛出的异常。

    使用场景：会话非活跃、assistant 回复内容对账冲突、turn 已绑定其他
    Agent Server 运行、摘要源区间被占用等场景抛出。
    """

    pass


class IdempotencyConflict(RuntimeError):
    """幂等键被复用于不同请求时抛出的异常。

    使用场景：begin_turn 中同一（租户，主体，幂等键）已有 turn，但会话或
    request_hash 与本次请求不一致时抛出，BFF 层据此返回 409。
    """

    pass


class ConversationRepository(Protocol):
    """会话日志仓库的接口协议，定义上层依赖的最小能力集。

    使用场景：应用层与编排层面向该协议编程；SqlAlchemyConversationRepository
    是其标准实现，测试中可替换为内存实现。

    方法说明：
        create_conversation: 创建会话并返回记录。
        get_owned: 按归属读取会话，不存在时抛 ConversationNotFound。
        begin_turn: 幂等开启 turn 并写入用户消息。
        bind_server_run: 将 turn 绑定到 Agent Server 运行并更新状态。
        get_turn_owned: 按 run_id 与归属读取 turn。
        list_messages: 按会话读取原文消息（默认仅可见）。
        list_summaries: 按会话读取摘要（默认仅 ACTIVE）。
        get_summary: 按主键读取摘要。
        save_manifest: 按 model_call_id 幂等保存模型调用 Manifest。
    """

    def create_conversation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        agent_id: str,
        agent_profile_version: str,
        conversation_id: str | None = None,
        agent_thread_id: str | None = None,
    ) -> Conversation:
        """创建新会话并返回其记录，会话 ID 与线程 ID 缺省时自动生成。"""
        ...

    def get_owned(self, conversation_id: str, tenant_id: str, subject_id: str) -> Conversation:
        """按（会话 ID，租户，主体）读取会话记录，不存在时抛出异常。"""
        ...

    def begin_turn(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        message: str,
        target_type: str,
        target_id: str,
        target_version: str,
    ) -> tuple[ConversationTurn, ConversationMessage, bool]:
        """幂等开启 turn 并写入用户消息，返回（turn，用户消息，是否幂等重放）。"""
        ...

    def bind_server_run(self, turn_id: str, server_run_id: str, status: str) -> ConversationTurn:
        """将 turn 绑定到指定的 Agent Server 运行并更新其状态。"""
        ...

    def get_turn_owned(self, run_id: str, tenant_id: str, subject_id: str) -> ConversationTurn:
        """按 run_id 与归属读取 turn，不存在时抛出异常。"""
        ...

    def list_messages(
        self, conversation_id: str, *, visible_only: bool = True
    ) -> tuple[ConversationMessage, ...]:
        """按序号升序返回会话的原文消息，默认仅包含可见消息。"""
        ...

    def list_summaries(
        self, conversation_id: str, *, active_only: bool = True
    ) -> tuple[ConversationSummary, ...]:
        """按层级与起始序号返回会话摘要，默认仅包含 ACTIVE 状态。"""
        ...

    def get_summary(self, summary_id: str) -> ConversationSummary:
        """按主键读取摘要记录，不存在时抛出异常。"""
        ...

    def save_manifest(self, manifest: ModelContextManifest) -> ModelContextManifest:
        """按 model_call_id 幂等保存模型调用 Manifest，返回已保存记录。"""
        ...


def content_hash(content: str) -> str:
    """计算文本内容的 SHA-256 十六进制摘要。

    使用场景：消息与摘要落库前生成 content_hash，供幂等对账与冲突检测使用。

    Args:
        content: 原始文本。

    Returns:
        str: 64 位十六进制摘要字符串。

    """
    return sha256(content.encode()).hexdigest()


def _conversation(row: ConversationRow) -> Conversation:
    """将 ConversationRow 表记录转换为 Conversation 领域记录。"""
    return Conversation(
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        agent_id=row.agent_id,
        agent_profile_version=row.agent_profile_version,
        agent_thread_id=row.agent_thread_id,
        status=ConversationStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn(row: ConversationTurnRow) -> ConversationTurn:
    """将 ConversationTurnRow 表记录转换为 ConversationTurn 领域记录。"""
    return ConversationTurn(
        turn_id=row.turn_id,
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        run_id=row.run_id,
        server_run_id=row.server_run_id,
        client_idempotency_key=row.client_idempotency_key,
        request_hash=row.request_hash,
        target_type=row.target_type,
        target_id=row.target_id,
        target_version=row.target_version,
        status=TurnStatus(row.status),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def _message(row: ConversationMessageRow) -> ConversationMessage:
    """将 ConversationMessageRow 表记录转换为 ConversationMessage 领域记录。"""
    return ConversationMessage(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        sequence=row.sequence,
        parent_message_id=row.parent_message_id,
        role=MessageRole(row.role),
        content=row.content,
        content_hash=row.content_hash,
        visible=row.visible,
        created_at=row.created_at,
    )


def _summary(row: ConversationSummaryRow) -> ConversationSummary:
    """将 ConversationSummaryRow 表记录转换为 ConversationSummary 领域记录。"""
    return ConversationSummary(
        summary_id=row.summary_id,
        conversation_id=row.conversation_id,
        level=row.level,
        start_sequence=row.start_sequence,
        end_sequence=row.end_sequence,
        source_message_ids=tuple(row.source_message_ids),
        source_summary_ids=tuple(row.source_summary_ids),
        summary_content=row.summary_content,
        topics=tuple(row.topics),
        entities=tuple(row.entities),
        decisions=tuple(row.decisions),
        open_items=tuple(row.open_items),
        model_profile_version=row.model_profile_version,
        template_version=row.template_version,
        content_hash=row.content_hash,
        status=SummaryStatus(row.status),
        superseded_by=row.superseded_by,
        created_at=row.created_at,
    )


def _manifest(row: ModelContextManifestRow) -> ModelContextManifest:
    """将 ModelContextManifestRow 表记录转换为 ModelContextManifest（含嵌套校验）。"""
    return ModelContextManifest.model_validate(
        {
            "manifest_id": row.manifest_id,
            "model_call_id": row.model_call_id,
            "conversation_id": row.conversation_id,
            "turn_id": row.turn_id,
            "run_id": row.run_id,
            "prompt_template_version": row.prompt_template_version,
            "agent_profile_version": row.agent_profile_version,
            "model_profile_version": row.model_profile_version,
            "recent_message_start": row.recent_message_start,
            "recent_message_end": row.recent_message_end,
            "summary_ids": row.summary_ids,
            "memory_ids": row.memory_ids,
            "memory_refs": row.memory_refs,
            "historical_message_ids": row.historical_message_ids,
            "tool_result_refs": row.tool_result_refs,
            "exposed_tools": row.exposed_tools,
            "input_token_count": row.input_token_count,
            "available_input_tokens": row.available_input_tokens,
            "omissions": row.omissions,
            "context_hash": row.context_hash,
            "created_at": row.created_at,
        }
    )


class SqlAlchemyConversationRepository:
    """基于 SQLAlchemy 的会话日志仓库实现，每个操作使用独立事务。

    使用场景：bootstrap 阶段用 sessionmaker 构造并注入应用与编排层；写操作通过
    with_for_update 行锁与唯一约束保证幂等与并发安全，读操作走普通会话。

    Attributes:
        _sessions: SQLAlchemy sessionmaker 工厂；写操作用 begin() 开启事务，
            读操作直接调用工厂获取会话。

    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """保存会话工厂；所有读写操作都经由该工厂获取数据库会话。"""
        self._sessions = sessions

    def create_conversation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        agent_id: str,
        agent_profile_version: str,
        conversation_id: str | None = None,
        agent_thread_id: str | None = None,
    ) -> Conversation:
        """创建新会话记录并落库，缺省时自动生成会话与线程标识。

        使用场景：BFF 的"创建 Conversation"入口调用；agent_thread_id 必须为
        UUID 字符串，保证与 LangGraph 线程一一对应。

        Args:
            tenant_id: 租户标识。
            subject_id: 主体标识。
            agent_id: Agent 标识。
            agent_profile_version: Agent Profile 版本。
            conversation_id: 指定会话 ID；缺省时自动生成 "conversation-<hex>"。
            agent_thread_id: 指定 Agent 线程 UUID；缺省时自动生成。

        Returns:
            Conversation: 新建会话的领域记录。

        Raises:
            ValueError: agent_thread_id 不是合法 UUID 时抛出。

        """
        # 1. 生成时间戳，并将线程 ID 规范化为标准 UUID 字符串。
        now = datetime.now(UTC)
        resolved_thread_id = agent_thread_id or str(uuid4())
        try:
            resolved_thread_id = str(UUID(resolved_thread_id))
        except ValueError as exc:
            raise ValueError("agent_thread_id must be a UUID") from exc
        # 2. 构造会话行记录（默认 ACTIVE 状态），在独立事务中写入。
        row = ConversationRow(
            conversation_id=conversation_id or f"conversation-{uuid4().hex}",
            tenant_id=tenant_id,
            subject_id=subject_id,
            agent_id=agent_id,
            agent_profile_version=agent_profile_version,
            agent_thread_id=resolved_thread_id,
            status=ConversationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            session.add(row)
        return _conversation(row)

    def get_owned(self, conversation_id: str, tenant_id: str, subject_id: str) -> Conversation:
        """按（会话 ID，租户，主体）读取会话记录。

        Args:
            conversation_id: 会话标识。
            tenant_id: 租户标识。
            subject_id: 主体标识。

        Returns:
            Conversation: 会话领域记录。

        Raises:
            ConversationNotFound: 记录不存在或不属于该归属时抛出。

        """
        statement = select(ConversationRow).where(
            ConversationRow.conversation_id == conversation_id,
            ConversationRow.tenant_id == tenant_id,
            ConversationRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ConversationNotFound("conversation was not found for authenticated owner")
            return _conversation(row)

    def begin_turn(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        message: str,
        target_type: str,
        target_id: str,
        target_version: str,
    ) -> tuple[ConversationTurn, ConversationMessage, bool]:
        """幂等开启 turn 并写入用户消息，返回（turn，用户消息，是否幂等重放）。

        使用场景：BFF 的 message-only Turn 写入口；同一（租户，主体，幂等键）
        重复请求时返回已有 turn 与用户消息且重放标记为 True，保证日志不重复。

        Args:
            conversation_id: 目标会话标识。
            tenant_id: 租户标识。
            subject_id: 主体标识。
            idempotency_key: 客户端幂等键。
            request_hash: 请求内容哈希。
            message: 用户消息原文。
            target_type: 目标对象类型。
            target_id: 目标对象标识。
            target_version: 目标对象版本。

        Returns:
            tuple[ConversationTurn, ConversationMessage, bool]:
                turn 记录、该 turn 的用户消息，以及是否命中幂等重放。

        Raises:
            IdempotencyConflict: 幂等键已用于其他会话或不同请求内容时抛出。
            ConversationNotFound: 会话不存在或不属于该归属时抛出。
            ConversationConflict: 会话非活跃，或幂等 turn 缺少用户消息时抛出。

        """
        with self._sessions.begin() as session:
            # 1. 按（租户，主体，幂等键）查询既有 turn，命中则校验后重放返回。
            existing = session.scalar(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.tenant_id == tenant_id,
                    ConversationTurnRow.subject_id == subject_id,
                    ConversationTurnRow.client_idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                # 2. 校验幂等键未被复用于其他会话或不同请求内容。
                if (
                    existing.conversation_id != conversation_id
                    or existing.request_hash != request_hash
                ):
                    raise IdempotencyConflict(
                        "idempotency key was already used for another conversation request"
                    )
                # 3. 查找幂等 turn 的用户消息，缺失说明历史数据异常。
                user_message = session.scalar(
                    select(ConversationMessageRow).where(
                        ConversationMessageRow.turn_id == existing.turn_id,
                        ConversationMessageRow.role == MessageRole.USER.value,
                    )
                )
                if user_message is None:
                    raise ConversationConflict("idempotent turn is missing its user message")
                return _turn(existing), _message(user_message), True

            # 4. 行锁读取会话并校验存在性与活跃状态。
            conversation = session.scalar(
                select(ConversationRow)
                .where(
                    ConversationRow.conversation_id == conversation_id,
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.subject_id == subject_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise ConversationNotFound("conversation was not found for authenticated owner")
            if conversation.status != ConversationStatus.ACTIVE.value:
                raise ConversationConflict("conversation is not active")
            # 5. 计算会话内下一个消息序号，构造 turn 与用户消息记录。
            max_sequence = session.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.conversation_id == conversation_id
                )
            )
            now = datetime.now(UTC)
            turn_row = ConversationTurnRow(
                turn_id=f"turn-{uuid4().hex}",
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                run_id=f"run-{uuid4().hex}",
                client_idempotency_key=idempotency_key,
                request_hash=request_hash,
                target_type=target_type,
                target_id=target_id,
                target_version=target_version,
                status=TurnStatus.ACCEPTED.value,
                created_at=now,
            )
            message_row = ConversationMessageRow(
                message_id=f"message-{uuid4().hex}",
                conversation_id=conversation_id,
                turn_id=turn_row.turn_id,
                sequence=(max_sequence or 0) + 1,
                role=MessageRole.USER.value,
                content=message,
                content_hash=content_hash(message),
                visible=True,
                created_at=now,
            )
            # 6. 刷新会话更新时间并提交事务，返回新建记录与重放标记 False。
            conversation.updated_at = now
            session.add_all((turn_row, message_row))
        return _turn(turn_row), _message(message_row), False

    def bind_server_run(self, turn_id: str, server_run_id: str, status: str) -> ConversationTurn:
        """将 turn 绑定到 Agent Server 运行并更新状态，绑定后不可更改。

        Args:
            turn_id: turn 标识。
            server_run_id: Agent Server 运行标识。
            status: 目标状态字符串（经 _normalize_turn_status 归一化）。

        Returns:
            ConversationTurn: 更新后的 turn 记录。

        Raises:
            ConversationNotFound: turn 不存在时抛出。
            ConversationConflict: turn 已绑定其他运行时抛出。

        """
        with self._sessions.begin() as session:
            row = session.get(ConversationTurnRow, turn_id)
            if row is None:
                raise ConversationNotFound("turn was not found")
            if row.server_run_id is not None and row.server_run_id != server_run_id:
                raise ConversationConflict("turn is already bound to another Agent Server run")
            row.server_run_id = server_run_id
            row.status = _normalize_turn_status(status).value
        return _turn(row)

    def get_turn_owned(self, run_id: str, tenant_id: str, subject_id: str) -> ConversationTurn:
        """按 run_id 与归属读取 turn 记录。

        Args:
            run_id: 平台运行标识。
            tenant_id: 租户标识。
            subject_id: 主体标识。

        Returns:
            ConversationTurn: turn 领域记录。

        Raises:
            ConversationNotFound: 记录不存在或不属于该归属时抛出。

        """
        statement = select(ConversationTurnRow).where(
            ConversationTurnRow.run_id == run_id,
            ConversationTurnRow.tenant_id == tenant_id,
            ConversationTurnRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ConversationNotFound("turn was not found for authenticated owner")
            return _turn(row)

    def update_turn_status(self, run_id: str, status: str) -> ConversationTurn:
        """按 run_id 更新 turn 状态，到达终态时补记完成时间。

        使用场景：Agent Server 执行过程中推进状态机；COMPLETED/FAILED 视为终态。

        Args:
            run_id: 平台运行标识。
            status: 目标状态字符串（经 _normalize_turn_status 归一化）。

        Returns:
            ConversationTurn: 更新后的 turn 记录。

        Raises:
            ConversationNotFound: turn 不存在时抛出。

        """
        normalized = _normalize_turn_status(status)
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ConversationTurnRow).where(ConversationTurnRow.run_id == run_id)
            )
            if row is None:
                raise ConversationNotFound("turn was not found")
            row.status = normalized.value
            if normalized in {TurnStatus.COMPLETED, TurnStatus.FAILED}:
                row.completed_at = row.completed_at or datetime.now(UTC)
        return _turn(row)

    def append_assistant_message(
        self,
        *,
        run_id: str,
        content: str,
        parent_message_id: str | None = None,
    ) -> ConversationMessage:
        """为 turn 追加 assistant 回复消息并收敛 turn 状态，幂等可重放。

        使用场景：执行完成后写入最终回复；同 turn 同父消息重复调用且内容一致时
        返回既有消息，内容冲突则抛出对账异常。

        Args:
            run_id: 平台运行标识。
            content: assistant 回复原文。
            parent_message_id: 父消息标识；普通回复为 None，分支消息指定父消息。

        Returns:
            ConversationMessage: 新建或既有的 assistant 消息记录。

        Raises:
            ConversationNotFound: turn 或会话不存在时抛出。
            ConversationConflict: 同位置已存在内容不一致的回复时抛出。

        """
        digest = content_hash(content)
        with self._sessions.begin() as session:
            # 1. 行锁读取 turn，并查找同父消息的既有 assistant 回复。
            turn = session.scalar(
                select(ConversationTurnRow)
                .where(ConversationTurnRow.run_id == run_id)
                .with_for_update()
            )
            if turn is None:
                raise ConversationNotFound("turn was not found")
            existing = session.scalar(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.turn_id == turn.turn_id,
                    ConversationMessageRow.role == MessageRole.ASSISTANT.value,
                    ConversationMessageRow.parent_message_id == parent_message_id,
                )
            )
            # 2. 既有回复内容一致则幂等返回，不一致则视为对账冲突。
            if existing is not None:
                if existing.content_hash != digest:
                    raise ConversationConflict("assistant message reconciliation conflict")
                return _message(existing)
            # 3. 计算会话内下一个序号并写入消息，同时收敛 turn 与会话更新时间。
            conversation = session.get(ConversationRow, turn.conversation_id)
            if conversation is None:
                raise ConversationNotFound("conversation was not found")
            max_sequence = session.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.conversation_id == turn.conversation_id
                )
            )
            now = datetime.now(UTC)
            row = ConversationMessageRow(
                message_id=f"message-{uuid4().hex}",
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                sequence=(max_sequence or 0) + 1,
                parent_message_id=parent_message_id,
                role=MessageRole.ASSISTANT.value,
                content=content,
                content_hash=digest,
                visible=True,
                created_at=now,
            )
            turn.status = TurnStatus.COMPLETED.value
            turn.completed_at = turn.completed_at or now
            conversation.updated_at = now
            session.add(row)
        return _message(row)

    def append_branch_message(
        self,
        *,
        run_id: str,
        content: str,
        parent_message_id: str,
    ) -> ConversationMessage:
        """为分支场景追加 assistant 消息（委托给 append_assistant_message 实现）。

        Args:
            run_id: 平台运行标识。
            content: 分支回复原文。
            parent_message_id: 必填的父消息标识，标志该回复属于某条分支。

        Returns:
            ConversationMessage: 新建或既有的分支消息记录。

        """
        return self.append_assistant_message(
            run_id=run_id, content=content, parent_message_id=parent_message_id
        )

    def list_messages(
        self, conversation_id: str, *, visible_only: bool = True
    ) -> tuple[ConversationMessage, ...]:
        """按序号升序返回会话的全部原文消息。

        Args:
            conversation_id: 会话标识。
            visible_only: 为 True（默认）时仅返回可见消息。

        Returns:
            tuple[ConversationMessage, ...]: 按序号升序排列的消息元组。

        """
        statement: Select[tuple[ConversationMessageRow]] = select(ConversationMessageRow).where(
            ConversationMessageRow.conversation_id == conversation_id
        )
        if visible_only:
            statement = statement.where(ConversationMessageRow.visible.is_(True))
        statement = statement.order_by(ConversationMessageRow.sequence)
        with self._sessions() as session:
            return tuple(_message(row) for row in session.scalars(statement))

    def list_incomplete_turns(self) -> tuple[ConversationTurn, ...]:
        """返回所有未到达终态（非 COMPLETED/FAILED）的 turn。

        使用场景：Agent Server 重启后对账，据此恢复或收尾中断的轮次。

        Returns:
            tuple[ConversationTurn, ...]: 未完成 turn 的元组。

        """
        statement = select(ConversationTurnRow).where(
            ConversationTurnRow.status.not_in((TurnStatus.COMPLETED.value, TurnStatus.FAILED.value))
        )
        with self._sessions() as session:
            return tuple(_turn(row) for row in session.scalars(statement))

    def save_summary(
        self, summary: ConversationSummary, *, supersede_ids: Sequence[str] = ()
    ) -> ConversationSummary:
        """幂等保存摘要，并按需将既有摘要标记为被取代。

        使用场景：SummaryService 写入分段/分层摘要；同范围已有内容一致的
        ACTIVE 摘要时直接返回既有记录，内容不同则视为冲突。

        Args:
            summary: 待保存的摘要记录。
            supersede_ids: 需要被本次摘要取代的既有摘要 ID 序列，默认为空。

        Returns:
            ConversationSummary: 已保存或既有的摘要记录。

        Raises:
            ConversationConflict: 同范围已存在内容不同的 ACTIVE 摘要，或待取代
                摘要不属于该会话时抛出。

        """
        with self._sessions.begin() as session:
            # 1. 查询同会话、同层级、同区间的 ACTIVE 摘要。
            same_range = session.scalar(
                select(ConversationSummaryRow).where(
                    ConversationSummaryRow.conversation_id == summary.conversation_id,
                    ConversationSummaryRow.level == summary.level,
                    ConversationSummaryRow.start_sequence == summary.start_sequence,
                    ConversationSummaryRow.end_sequence == summary.end_sequence,
                    ConversationSummaryRow.status == SummaryStatus.ACTIVE.value,
                )
            )
            # 2. 已存在且内容一致时幂等返回；内容不同则视为冲突。
            if same_range is not None and same_range.summary_id not in supersede_ids:
                if same_range.content_hash == summary.content_hash:
                    return _summary(same_range)
                raise ConversationConflict("active summary already exists for source range")
            # 3. 写入新摘要，并将待取代摘要置为 SUPERSEDED 且记录取代者。
            row = ConversationSummaryRow(
                summary_id=summary.summary_id,
                conversation_id=summary.conversation_id,
                level=summary.level,
                start_sequence=summary.start_sequence,
                end_sequence=summary.end_sequence,
                source_message_ids=list(summary.source_message_ids),
                source_summary_ids=list(summary.source_summary_ids),
                summary_content=summary.summary_content,
                topics=list(summary.topics),
                entities=list(summary.entities),
                decisions=list(summary.decisions),
                open_items=list(summary.open_items),
                model_profile_version=summary.model_profile_version,
                template_version=summary.template_version,
                content_hash=summary.content_hash,
                status=SummaryStatus.ACTIVE.value,
                created_at=summary.created_at,
            )
            session.add(row)
            session.flush()
            for summary_id in supersede_ids:
                old = session.get(ConversationSummaryRow, summary_id)
                if old is None or old.conversation_id != summary.conversation_id:
                    raise ConversationConflict("summary rebuild source does not match conversation")
                old.status = SummaryStatus.SUPERSEDED.value
                old.superseded_by = summary.summary_id
        return summary

    def list_summaries(
        self, conversation_id: str, *, active_only: bool = True
    ) -> tuple[ConversationSummary, ...]:
        """按层级与起始序号升序返回会话摘要。

        Args:
            conversation_id: 会话标识。
            active_only: 为 True（默认）时仅返回 ACTIVE 状态摘要。

        Returns:
            tuple[ConversationSummary, ...]: 摘要记录元组。

        """
        statement = select(ConversationSummaryRow).where(
            ConversationSummaryRow.conversation_id == conversation_id
        )
        if active_only:
            statement = statement.where(ConversationSummaryRow.status == SummaryStatus.ACTIVE.value)
        statement = statement.order_by(
            ConversationSummaryRow.level,
            ConversationSummaryRow.start_sequence,
        )
        with self._sessions() as session:
            return tuple(_summary(row) for row in session.scalars(statement))

    def get_summary(self, summary_id: str) -> ConversationSummary:
        """按主键读取摘要记录。

        Args:
            summary_id: 摘要标识。

        Returns:
            ConversationSummary: 摘要领域记录。

        Raises:
            ConversationNotFound: 摘要不存在时抛出。

        """
        with self._sessions() as session:
            row = session.get(ConversationSummaryRow, summary_id)
            if row is None:
                raise ConversationNotFound("summary was not found")
            return _summary(row)

    def save_manifest(self, manifest: ModelContextManifest) -> ModelContextManifest:
        """按 model_call_id 幂等保存模型调用 Manifest。

        使用场景：每次模型调用前持久化上下文清单；同 model_call_id 重复保存且
        上下文一致时返回既有记录，不一致则抛出冲突。

        Args:
            manifest: 待保存的 Manifest 记录。

        Returns:
            ModelContextManifest: 已保存或既有的 Manifest 记录。

        Raises:
            ConversationConflict: 同 model_call_id 已存在但上下文哈希不一致时抛出。

        """
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ModelContextManifestRow).where(
                    ModelContextManifestRow.model_call_id == manifest.model_call_id
                )
            )
            if existing is not None:
                if existing.context_hash != manifest.context_hash:
                    raise ConversationConflict("model_call_id has conflicting context manifest")
                return _manifest(existing)
            row = ModelContextManifestRow(
                manifest_id=manifest.manifest_id,
                model_call_id=manifest.model_call_id,
                conversation_id=manifest.conversation_id,
                turn_id=manifest.turn_id,
                run_id=manifest.run_id,
                prompt_template_version=manifest.prompt_template_version,
                agent_profile_version=manifest.agent_profile_version,
                model_profile_version=manifest.model_profile_version,
                recent_message_start=manifest.recent_message_start,
                recent_message_end=manifest.recent_message_end,
                summary_ids=list(manifest.summary_ids),
                memory_ids=list(manifest.memory_ids),
                memory_refs=[item.model_dump(mode="json") for item in manifest.memory_refs],
                historical_message_ids=list(manifest.historical_message_ids),
                tool_result_refs=list(manifest.tool_result_refs),
                exposed_tools=list(manifest.exposed_tools),
                input_token_count=manifest.input_token_count,
                available_input_tokens=manifest.available_input_tokens,
                omissions=[item.model_dump(mode="json") for item in manifest.omissions],
                context_hash=manifest.context_hash,
                created_at=manifest.created_at,
            )
            session.add(row)
        return manifest

    def list_manifests(self, conversation_id: str) -> tuple[ModelContextManifest, ...]:
        """按创建时间升序返回会话的全部 Manifest。

        使用场景：审计与调试时回放一次会话内每次模型调用的上下文构成。

        Args:
            conversation_id: 会话标识。

        Returns:
            tuple[ModelContextManifest, ...]: 按时间升序排列的 Manifest 元组。

        """
        statement = (
            select(ModelContextManifestRow)
            .where(ModelContextManifestRow.conversation_id == conversation_id)
            .order_by(ModelContextManifestRow.created_at)
        )
        with self._sessions() as session:
            return tuple(_manifest(row) for row in session.scalars(statement))


def _normalize_turn_status(status: str) -> TurnStatus:
    """将外部状态字符串归一化为 TurnStatus，未知取值回落为 PENDING。

    使用场景：bind_server_run 与 update_turn_status 入库前统一状态口径，
    兼容 "success"/"error" 等运行时同义词。

    Args:
        status: 外部状态字符串。

    Returns:
        TurnStatus: 归一化后的状态；未识别时返回 TurnStatus.PENDING。

    """
    mapping = {
        "accepted": TurnStatus.ACCEPTED,
        "pending": TurnStatus.PENDING,
        "running": TurnStatus.RUNNING,
        "waiting_child": TurnStatus.WAITING_CHILD,
        "interrupted": TurnStatus.INTERRUPTED,
        "success": TurnStatus.COMPLETED,
        "completed": TurnStatus.COMPLETED,
        "error": TurnStatus.FAILED,
        "failed": TurnStatus.FAILED,
    }
    return mapping.get(status, TurnStatus.PENDING)
