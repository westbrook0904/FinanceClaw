"""会话日志的持久化仓库：负责领域模型与 ORM 表的互转及事务落库。

提供会话、turn、消息与 Manifest 的幂等写入与归属查询能力，
是 Conversation Journal 唯一的数据库访问层。
"""

from contextlib import nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.kernel.turn_status import TurnStatus
from financeclaw.shared.conversation.models import (
    ChannelConversationBinding,
    Conversation,
    ConversationMessage,
    ConversationStatus,
    ConversationTurn,
    MessageRole,
    ModelContextManifest,
)
from financeclaw.shared.conversation.tables import (
    ChannelConversationBindingRow,
    ConversationMessageRow,
    ConversationRow,
    ModelContextManifestRow,
)
from financeclaw.shared.turns.tables import ConversationTurnRow


class ConversationNotFound(LookupError):
    """按归属查询的会话资源不存在时抛出的异常。

    使用场景：get_owned、get_turn_owned 等方法在目标记录不存在时抛出，
    API 层通常据此返回 404。
    """

    pass


class ConversationConflict(RuntimeError):
    """会话状态或内容与操作前提冲突时抛出的异常。

    使用场景：会话非活跃、assistant 回复内容对账冲突、turn 已绑定其他
    Agent Server 运行等场景抛出。
    """

    pass


class IdempotencyConflict(RuntimeError):
    """幂等键被复用于不同请求时抛出的异常。

    使用场景：begin_turn 中同一（租户，主体，幂等键）已有 turn，但会话或
    request_hash 与本次请求不一致时抛出，API 层据此返回 409。
    """

    pass


class ConversationRepository(Protocol):
    """会话日志仓库的接口协议，定义上层依赖的最小能力集。

    使用场景：应用层与编排层面向该协议编程；SqlAlchemyConversationRepository
    是其标准实现，测试中可替换为内存实现。

    方法说明：
        create_conversation: 创建会话并返回记录。
        get_channel_binding: 按外部单聊键读取绑定。
        get_or_create_channel_conversation: 原子解析或创建单聊绑定及会话。
        get_owned: 按归属读取会话，不存在时抛 ConversationNotFound。
        begin_turn: 幂等开启 turn 并写入用户消息。
        bind_server_run: 将 turn 绑定到 Agent Server 运行并更新状态。
        get_turn_owned: 按 turn_id 与归属读取 turn。
        list_messages: 按会话读取原文消息（默认仅可见）。
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

    def get_message_owned(
        self, message_id: str, tenant_id: str, subject_id: str
    ) -> ConversationMessage:
        """精确读取主体自己的来源消息，不扫描整段会话。"""
        ...

    def messages_for_turn(
        self, conversation_id: str, turn_id: str
    ) -> tuple[ConversationMessage, ...]:
        """返回一个 Turn 的可见顶层问答；调用方先校验会话归属。"""
        ...

    def completed_history(
        self, conversation_id: str, *, before_sequence: int, turns: int
    ) -> tuple[ConversationMessage, ...]:
        """新 thread 初始化时使用的有界已完成问答。"""
        ...

    def get_channel_binding(
        self,
        *,
        channel: str,
        app_id: str,
        tenant_key: str,
        external_chat_id: str,
    ) -> ChannelConversationBinding | None:
        """按 Channel 单聊唯一键读取绑定；不存在时返回 ``None``。"""
        ...

    def get_or_create_channel_conversation(
        self,
        *,
        channel: str,
        app_id: str,
        tenant_key: str,
        external_user_id: str,
        external_chat_id: str,
        tenant_id: str,
        subject_id: str,
        agent_id: str,
        agent_profile_version: str,
    ) -> tuple[ChannelConversationBinding, Conversation, bool]:
        """原子读取或创建 Channel 绑定和会话，返回是否新建。"""
        ...

    def get_turn_owned(self, turn_id: str, tenant_id: str, subject_id: str) -> ConversationTurn:
        """按 turn_id 与归属读取 turn，不存在时抛出异常。"""
        ...

    def list_messages(
        self,
        conversation_id: str,
        *,
        visible_only: bool = True,
        after: int = 0,
        limit: int | None = None,
    ) -> tuple[ConversationMessage, ...]:
        """按序号升序返回会话的原文消息，默认仅包含可见消息。"""
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


def _channel_binding(row: ChannelConversationBindingRow) -> ChannelConversationBinding:
    """将 ChannelConversationBindingRow 转换为领域绑定记录。"""
    return ChannelConversationBinding(
        binding_id=row.binding_id,
        channel=row.channel,
        app_id=row.app_id,
        tenant_key=row.tenant_key,
        external_user_id=row.external_user_id,
        external_chat_id=row.external_chat_id,
        conversation_id=row.conversation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn(row: ConversationTurnRow) -> ConversationTurn:
    """Return the Journal-facing projection without exposing grants or execution state."""
    return ConversationTurn.model_validate(
        {key: getattr(row, key) for key in ConversationTurn.model_fields}
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


def _manifest(row: ModelContextManifestRow) -> ModelContextManifest:
    """将 ModelContextManifestRow 表记录转换为 ModelContextManifest（含嵌套校验）。"""
    return ModelContextManifest.model_validate(
        {
            column.name: getattr(row, column.name)
            for column in ModelContextManifestRow.__table__.columns
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
        from financeclaw.shared.turns.budget import TurnExecutionRepository

        self.execution = TurnExecutionRepository(sessions)

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

        使用场景：API 的"创建 Conversation"入口调用；agent_thread_id 必须为
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

    def get_channel_binding(
        self,
        *,
        channel: str,
        app_id: str,
        tenant_key: str,
        external_chat_id: str,
    ) -> ChannelConversationBinding | None:
        """按外部单聊唯一键读取 Conversation 绑定。

        Args:
            channel: Channel 类型。
            app_id: 外部应用 ID。
            tenant_key: 外部租户键。
            external_chat_id: 外部单聊 ID。

        Returns:
            已存在的绑定；找不到时返回 ``None``。

        """
        statement = select(ChannelConversationBindingRow).where(
            ChannelConversationBindingRow.channel == channel,
            ChannelConversationBindingRow.app_id == app_id,
            ChannelConversationBindingRow.tenant_key == tenant_key,
            ChannelConversationBindingRow.external_chat_id == external_chat_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            return _channel_binding(row) if row is not None else None

    def get_or_create_channel_conversation(
        self,
        *,
        channel: str,
        app_id: str,
        tenant_key: str,
        external_user_id: str,
        external_chat_id: str,
        tenant_id: str,
        subject_id: str,
        agent_id: str,
        agent_profile_version: str,
    ) -> tuple[ChannelConversationBinding, Conversation, bool]:
        """在一个事务内解析或新建 Channel 单聊绑定及其 Conversation。

        唯一约束负责跨线程竞态收敛；竞态失败方回滚孤立 Conversation 后重读
        胜出绑定。若同一个 chat 后续映射到不同用户身份则拒绝，避免身份串用。

        Args:
            channel: Channel 类型，一期传 ``feishu``。
            app_id: 飞书应用 ID。
            tenant_key: 飞书租户键。
            external_user_id: 发件人 open_id。
            external_chat_id: P2P chat_id。
            tenant_id: 映射后的 FinanceClaw 租户 ID。
            subject_id: 映射后的 FinanceClaw 主体 ID。
            agent_id: 新会话绑定的顶层 Agent ID。
            agent_profile_version: 新会话固定的 Agent Profile 版本。

        Returns:
            ``(binding, conversation, created)``；既有绑定时 created 为 False。

        Raises:
            ConversationConflict: 既有绑定身份不一致或并发创建无法收敛。

        """
        for _attempt in range(2):
            try:
                with self._sessions.begin() as session:
                    existing = session.scalar(
                        select(ChannelConversationBindingRow)
                        .where(
                            ChannelConversationBindingRow.channel == channel,
                            ChannelConversationBindingRow.app_id == app_id,
                            ChannelConversationBindingRow.tenant_key == tenant_key,
                            ChannelConversationBindingRow.external_chat_id == external_chat_id,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        conversation = session.get(ConversationRow, existing.conversation_id)
                        if conversation is None:
                            raise ConversationConflict(
                                "channel binding references a missing conversation"
                            )
                        if (
                            existing.external_user_id != external_user_id
                            or conversation.tenant_id != tenant_id
                            or conversation.subject_id != subject_id
                        ):
                            raise ConversationConflict(
                                "channel chat is already bound to another verified identity"
                            )
                        existing.updated_at = datetime.now(UTC)
                        session.flush()
                        return _channel_binding(existing), _conversation(conversation), False

                    now = datetime.now(UTC)
                    conversation = ConversationRow(
                        conversation_id=f"conversation-{uuid4().hex}",
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        agent_id=agent_id,
                        agent_profile_version=agent_profile_version,
                        agent_thread_id=str(uuid4()),
                        status=ConversationStatus.ACTIVE.value,
                        created_at=now,
                        updated_at=now,
                    )
                    binding = ChannelConversationBindingRow(
                        binding_id=f"binding-{uuid4().hex}",
                        channel=channel,
                        app_id=app_id,
                        tenant_key=tenant_key,
                        external_user_id=external_user_id,
                        external_chat_id=external_chat_id,
                        conversation_id=conversation.conversation_id,
                        created_at=now,
                        updated_at=now,
                    )
                    # 显式先刷 Conversation，避免未声明 ORM relationship 时绑定外键先写。
                    session.add(conversation)
                    session.flush()
                    session.add(binding)
                    session.flush()
                    return _channel_binding(binding), _conversation(conversation), True
            except IntegrityError:
                # 并发插入相同唯一键时当前事务已回滚；下一轮读取胜出记录。
                continue
        raise ConversationConflict("concurrent channel binding creation did not converge")

    def get_turn_owned(self, turn_id: str, tenant_id: str, subject_id: str) -> ConversationTurn:
        """按 turn_id 与归属读取 turn 记录。

        Args:
            turn_id: 平台运行标识。
            tenant_id: 租户标识。
            subject_id: 主体标识。

        Returns:
            ConversationTurn: turn 领域记录。

        Raises:
            ConversationNotFound: 记录不存在或不属于该归属时抛出。

        """
        statement = select(ConversationTurnRow).where(
            ConversationTurnRow.turn_id == turn_id,
            ConversationTurnRow.tenant_id == tenant_id,
            ConversationTurnRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ConversationNotFound("turn was not found for authenticated owner")
            return _turn(row)

    def append_assistant_message(
        self,
        *,
        turn_id: str,
        content: str,
        parent_message_id: str | None = None,
        session: Session | None = None,
    ) -> ConversationMessage:
        """为 turn 追加 assistant 回复消息并收敛 turn 状态，幂等可重放。

        使用场景：执行完成后写入最终回复；同 turn 同父消息重复调用且内容一致时
        返回既有消息，内容冲突则抛出对账异常。

        Args:
            turn_id: 平台运行标识。
            content: assistant 回复原文。
            parent_message_id: 父消息标识；普通回复为 None，分支消息指定父消息。
            session: 外层组合事务；提供时不另建连接、不自行提交。

        Returns:
            ConversationMessage: 新建或既有的 assistant 消息记录。

        Raises:
            ConversationNotFound: turn 或会话不存在时抛出。
            ConversationConflict: 同位置已存在内容不一致的回复时抛出。

        """
        digest = content_hash(content)
        with nullcontext(session) if session is not None else self._sessions.begin() as session:
            session.execute(
                update(ConversationRow)
                .where(
                    ConversationRow.conversation_id
                    == select(ConversationTurnRow.conversation_id)
                    .where(ConversationTurnRow.turn_id == turn_id)
                    .scalar_subquery(),
                )
                .values(updated_at=ConversationRow.updated_at)
            )
            # 1. 行锁读取 turn，并查找同父消息的既有 assistant 回复。
            turn = session.scalar(
                select(ConversationTurnRow)
                .where(ConversationTurnRow.turn_id == turn_id)
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
            if turn.status in {"failed", "cancelled", "cancelling"}:
                raise ConversationConflict("cancelled or failed turn cannot append a final answer")
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
            conversation.updated_at = now
            session.add(row)
            if parent_message_id is None:
                from financeclaw.shared.conversation.indexing import enqueue_history_index

                user = session.scalar(
                    select(ConversationMessageRow).where(
                        ConversationMessageRow.turn_id == turn.turn_id,
                        ConversationMessageRow.role == MessageRole.USER.value,
                    )
                )
                enqueue_history_index(session, turn, user, row)
        return _message(row)

    def append_branch_message(
        self,
        *,
        turn_id: str,
        content: str,
        parent_message_id: str,
    ) -> ConversationMessage:
        """为分支场景追加 assistant 消息（子图调用给 append_assistant_message 实现）。

        Args:
            turn_id: 平台运行标识。
            content: 分支回复原文。
            parent_message_id: 必填的父消息标识，标志该回复属于某条分支。

        Returns:
            ConversationMessage: 新建或既有的分支消息记录。

        """
        return self.append_assistant_message(
            turn_id=turn_id, content=content, parent_message_id=parent_message_id
        )

    def get_message_owned(
        self, message_id: str, tenant_id: str, subject_id: str
    ) -> ConversationMessage:
        """按消息 ID 精确读取可信证据，不扫描整个 Journal。"""
        statement = (
            select(ConversationMessageRow)
            .join(ConversationRow)
            .where(
                ConversationMessageRow.message_id == message_id,
                ConversationRow.tenant_id == tenant_id,
                ConversationRow.subject_id == subject_id,
            )
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ConversationNotFound("message was not found for authenticated owner")
            return _message(row)

    def messages_for_turn(
        self, conversation_id: str, turn_id: str
    ) -> tuple[ConversationMessage, ...]:
        """读取一个 Turn 的原始问答；调用者须先校验会话归属。"""
        statement = (
            select(ConversationMessageRow)
            .where(
                ConversationMessageRow.conversation_id == conversation_id,
                ConversationMessageRow.turn_id == turn_id,
                ConversationMessageRow.parent_message_id.is_(None),
                ConversationMessageRow.visible.is_(True),
            )
            .order_by(ConversationMessageRow.sequence)
        )
        with self._sessions() as session:
            return tuple(_message(row) for row in session.scalars(statement))

    def completed_history(
        self, conversation_id: str, *, before_sequence: int, turns: int
    ) -> tuple[ConversationMessage, ...]:
        """初始化时有界读取已完成问答；排除失败 Turn 和分支草稿。"""
        if not 0 <= turns <= 100:
            raise ValueError("invalid bootstrap Turn limit")
        statement = (
            select(ConversationMessageRow)
            .join(
                ConversationTurnRow, ConversationTurnRow.turn_id == ConversationMessageRow.turn_id
            )
            .where(
                ConversationMessageRow.conversation_id == conversation_id,
                ConversationMessageRow.sequence < before_sequence,
                ConversationMessageRow.parent_message_id.is_(None),
                ConversationMessageRow.visible.is_(True),
                ConversationTurnRow.status == TurnStatus.COMPLETED.value,
            )
            .order_by(ConversationMessageRow.sequence.desc())
            .limit(turns * 2)
        )
        with self._sessions() as session:
            return tuple(reversed([_message(row) for row in session.scalars(statement)]))

    def list_messages(
        self,
        conversation_id: str,
        *,
        visible_only: bool = True,
        after: int = 0,
        limit: int | None = None,
    ) -> tuple[ConversationMessage, ...]:
        """按序号升序返回会话的全部原文消息。

        Args:
            conversation_id: 会话标识。
            visible_only: 为 True（默认）时仅返回可见消息。
            after: 仅返回此序号之后的消息。
            limit: 本页最多返回的消息条数。

        Returns:
            tuple[ConversationMessage, ...]: 按序号升序排列的消息元组。

        """
        statement: Select[tuple[ConversationMessageRow]] = select(ConversationMessageRow).where(
            ConversationMessageRow.conversation_id == conversation_id
        )
        if visible_only:
            statement = statement.where(ConversationMessageRow.visible.is_(True))
        statement = statement.order_by(ConversationMessageRow.sequence)
        statement = statement.where(ConversationMessageRow.sequence > after)
        if limit is not None:
            statement = statement.limit(limit)
        with self._sessions() as session:
            return tuple(_message(row) for row in session.scalars(statement))

    def list_incomplete_turns(self) -> tuple[ConversationTurn, ...]:
        """返回所有未到达终态（非 COMPLETED/FAILED）的 turn。

        使用场景：Agent Server 重启后对账，据此恢复或收尾中断的轮次。

        Returns:
            tuple[ConversationTurn, ...]: 未完成 turn 的元组。

        """
        statement = select(ConversationTurnRow).where(
            ConversationTurnRow.status.not_in(
                (TurnStatus.COMPLETED.value, TurnStatus.FAILED.value, TurnStatus.CANCELLED.value)
            )
        )
        with self._sessions() as session:
            return tuple(_turn(row) for row in session.scalars(statement))

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
                **manifest.model_dump(mode="json", exclude={"created_at"}),
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
