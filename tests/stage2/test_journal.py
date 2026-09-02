from pathlib import Path
from uuid import UUID

import pytest

from financeclaw.conversation import (
    ConversationConflict,
    ConversationNotFound,
    IdempotencyConflict,
    SqlAlchemyConversationRepository,
)
from financeclaw.infrastructure import ApplicationDatabase


def repository(path: Path) -> tuple[ApplicationDatabase, SqlAlchemyConversationRepository]:
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    database.initialize_schema()
    return database, SqlAlchemyConversationRepository(database.session_factory)


def test_journal_is_append_only_owned_idempotent_and_branchable(tmp_path: Path) -> None:
    database, journal = repository(tmp_path / "journal.db")
    conversation = journal.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    assert str(UUID(conversation.agent_thread_id)) == conversation.agent_thread_id
    turn, user, replay = journal.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="turn-key",
        request_hash="a" * 64,
        message="Compare AAPL with MSFT",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    same_turn, same_user, same_replay = journal.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="turn-key",
        request_hash="a" * 64,
        message="Compare AAPL with MSFT",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )

    assert not replay
    assert same_replay
    assert same_turn.turn_id == turn.turn_id
    assert same_user.message_id == user.message_id
    with pytest.raises(IdempotencyConflict):
        journal.begin_turn(
            conversation_id=conversation.conversation_id,
            tenant_id="tenant-a",
            subject_id="subject-a",
            idempotency_key="turn-key",
            request_hash="b" * 64,
            message="different",
            target_type="agent",
            target_id="finance_agent",
            target_version="1.0.0",
        )
    with pytest.raises(ConversationNotFound):
        journal.get_owned(conversation.conversation_id, "tenant-b", "subject-a")

    journal.bind_server_run(turn.turn_id, "server-run-1", "running")
    assistant = journal.append_assistant_message(run_id=turn.run_id, content="AAPL summary")
    assert (
        journal.append_assistant_message(run_id=turn.run_id, content="AAPL summary").message_id
        == assistant.message_id
    )
    with pytest.raises(ConversationConflict):
        journal.append_assistant_message(run_id=turn.run_id, content="conflicting replacement")
    branch = journal.append_branch_message(
        run_id=turn.run_id,
        content="Regenerated AAPL summary",
        parent_message_id=assistant.message_id,
    )
    messages = journal.list_messages(conversation.conversation_id)
    assert [item.sequence for item in messages] == [1, 2, 3]
    assert branch.parent_message_id == assistant.message_id
    assert messages[0].content == "Compare AAPL with MSFT"
    database.close()


def test_journal_survives_database_and_process_reconstruction(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    first_database, first = repository(path)
    conversation = first.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    turn, _, _ = first.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="restart-key",
        request_hash="c" * 64,
        message="Remember the old discussion",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    first.bind_server_run(turn.turn_id, "server-restart", "pending")
    first_database.close()

    second_database, second = repository(path)
    restored = second.get_owned(conversation.conversation_id, "tenant-a", "subject-a")
    restored_turn = second.get_turn_owned(turn.run_id, "tenant-a", "subject-a")
    assert restored.agent_thread_id == conversation.agent_thread_id
    assert restored.agent_profile_version == "1.0.0"
    assert restored_turn.server_run_id == "server-restart"
    assert second.list_messages(conversation.conversation_id)[0].content.startswith("Remember")
    second_database.close()
