"""Isolated memory authority fixtures shared by the migrated Stage 3/9 regressions."""

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import MemoryRepository
from financeclaw.shared.turns.tables import ConversationTurnRow
from tests.stage3.support import journal
from tests.turn_support import seed_execution


def create_domain(tmp_path):
    """Create a temporary application database with real SQL audit and outbox tables."""
    database, conversations = journal(tmp_path / "memory-domain.db")
    actor = MemoryActor(
        tenant_id="tenant.a",
        subject_id="subject.a",
        scopes={"memory:read", "memory:write", "memory:delete"},
    )
    return (
        database,
        conversations,
        actor,
        MemoryMutationService(database.session_factory),
        MemoryRepository(database.session_factory),
    )


def add_user_source(stack, text, key="source-turn", *, auto_commit=False):
    """Register a genuine user Journal source through the same admission boundary as API."""
    database, conversations, actor, _, _ = stack
    context, message_id = seed_user_journal(conversations, actor, text, key)
    actor = actor.model_copy(
        update={
            "conversation_id": context.conversation_id,
            "turn_id": context.turn_id,
            "agent_id": "finance_agent",
        }
    )
    with database.session_factory.begin() as session:
        source = MemoryIntake(
            database.session_factory, auto_commit_low_risk=auto_commit
        ).register_message_in_session(session, actor, message_id)
    return actor, source, context


def seed_user_journal(conversations, actor, text, key):
    """Create genuine business input without pre-registering the memory source under test."""
    conversation = conversations.create_conversation(
        tenant_id=actor.tenant_id,
        subject_id=actor.subject_id,
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    context = ExecutionContext(
        tenant_id=actor.tenant_id,
        subject_id=actor.subject_id,
        conversation_id=conversation.conversation_id,
        turn_id=key,
        scopes=actor.scopes,
    )
    context = seed_execution(
        conversations.execution,
        context,
        {"limits": {"model": 100, "tool": 100, "command": 100}},
        message=text,
    )
    with conversations._sessions() as session:
        message_id = session.get(ConversationTurnRow, key).user_message_id
    return context, message_id
