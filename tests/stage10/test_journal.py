"""Append-only Journal semantics with Stage 10 admission as the sole user-message writer."""

import pytest

from financeclaw.shared.conversation.repository import (
    ConversationConflict,
    ConversationNotFound,
    SqlAlchemyConversationRepository,
)
from financeclaw.shared.infrastructure.database import ApplicationDatabase


def test_journal_remains_owned_idempotent_and_branchable(service, admit):
    """Final answers cannot be replaced, while explicit branches preserve sequence and ancestry."""
    accepted = admit(message="Compare AAPL and MSFT")
    journal = service.journal
    with pytest.raises(ConversationNotFound):
        journal.get_owned(accepted.conversation_id, "foreign", "user")
    final = journal.append_assistant_message(turn_id=accepted.turn_id, content="summary")
    assert (
        journal.append_assistant_message(turn_id=accepted.turn_id, content="summary").message_id
        == final.message_id
    )
    with pytest.raises(ConversationConflict):
        journal.append_assistant_message(turn_id=accepted.turn_id, content="replacement")
    branch = journal.append_branch_message(
        turn_id=accepted.turn_id, content="alternative", parent_message_id=final.message_id
    )
    messages = journal.list_messages(accepted.conversation_id)
    assert [message.sequence for message in messages] == [1, 2, 3]
    assert branch.parent_message_id == final.message_id
    assert (
        journal.list_messages(accepted.conversation_id, after=1, limit=1)[0].message_id
        == final.message_id
    )


def test_journal_reconstructs_from_database(service, admit):
    """A new repository reads the same Turn and input without any in-memory execution state."""
    accepted = admit(message="Remember the discussion")
    engine = service.sessions.kw["bind"]
    database = ApplicationDatabase(str(engine.url))
    try:
        journal = SqlAlchemyConversationRepository(database.session_factory)
        turn = journal.get_turn_owned(accepted.turn_id, "tenant", "user")
        assert turn.turn_id == accepted.turn_id
        assert (
            journal.list_messages(accepted.conversation_id)[0].content == "Remember the discussion"
        )
    finally:
        database.close()
