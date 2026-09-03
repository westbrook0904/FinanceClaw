"""`support` 模块提供`stage3`相关能力。"""

from pathlib import Path

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.conversation import SqlAlchemyConversationRepository


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
) -> tuple[ExecutionContext, str]:
    """处理 `context`，并返回边界约定的结果。"""
    # 准备 conversation，供后续步骤使用。
    conversation = repository.create_conversation(
        tenant_id=tenant_id,
        subject_id=subject_id,
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    # 准备 turn and user_message and _，供后续步骤使用。
    turn, user_message, _ = repository.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id=tenant_id,
        subject_id=subject_id,
        idempotency_key=key,
        request_hash=(key.encode().hex() + "0" * 64)[:64],
        message=message,
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    # 向调用方返回符合边界约定的结果。
    return (
        ExecutionContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes={"memory:read", "memory:write", "memory:delete"},
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
        ),
        user_message.message_id,
    )
