"""`support` 模块提供`stage3`相关能力。"""

from pathlib import Path

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.infrastructure.database import ApplicationDatabase


def journal(path: Path) -> tuple[ApplicationDatabase, SqlAlchemyConversationRepository]:
    """处理 `当前操作`，并返回边界约定的结果。"""
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    database.initialize_schema()
    return database, SqlAlchemyConversationRepository(database.session_factory)


def conversation_context(
    repository: SqlAlchemyConversationRepository,
    *,
    tenant_id: str = "tenant.a",
    subject_id: str = "subject.a",
    message: str = "请记住我偏好低波动资产",
    key: str = "memory-turn",
    profile=None,
) -> tuple[ExecutionContext, str]:
    """处理 `context`，并返回边界约定的结果。"""
    # 准备 conversation，供后续步骤使用。
    conversation = repository.create_conversation(
        tenant_id=tenant_id,
        subject_id=subject_id,
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    from financeclaw.shared.turns.tables import ConversationTurnRow
    from tests.turn_support import seed_execution

    context = ExecutionContext(
        tenant_id=tenant_id,
        subject_id=subject_id,
        conversation_id=conversation.conversation_id,
        turn_id=key,
        scopes={"memory:read", "memory:write", "memory:delete"},
    )
    from financeclaw.shared.turns.snapshots import agent_snapshot

    snapshot = (
        agent_snapshot(profile, context, thread_id=conversation.agent_thread_id, input_hash=key)
        if profile
        else {"limits": {"model": 100, "tool": 100, "command": 100}}
    )
    context = seed_execution(repository.execution, context, snapshot, message=message)
    with repository._sessions() as session:
        message_id = session.get(ConversationTurnRow, context.turn_id).user_message_id
    return context, message_id
