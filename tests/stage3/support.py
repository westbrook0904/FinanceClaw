"""Small Journal fixtures shared by Stage-3 tests."""

from pathlib import Path

from financeclaw.contracts import ExecutionContext
from financeclaw.conversation import SqlAlchemyConversationRepository
from financeclaw.infrastructure import ApplicationDatabase


def journal(path: Path) -> tuple[ApplicationDatabase, SqlAlchemyConversationRepository]:
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
    conversation = repository.create_conversation(
        tenant_id=tenant_id,
        subject_id=subject_id,
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
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
