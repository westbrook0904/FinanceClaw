"""`test_journal` 模块提供`stage2`相关能力。"""

from pathlib import Path
from uuid import UUID

import pytest

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.modules.conversation import (
    ConversationConflict,
    ConversationNotFound,
    IdempotencyConflict,
    SqlAlchemyConversationRepository,
)


def repository(path: Path) -> tuple[ApplicationDatabase, SqlAlchemyConversationRepository]:
    """处理 `当前操作`，并返回边界约定的结果。"""
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    database.initialize_schema()
    return database, SqlAlchemyConversationRepository(database.session_factory)


def test_journal_is_append_only_owned_idempotent_and_branchable(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and journal，供后续步骤使用。
    database, journal = repository(tmp_path / "journal.db")
    # 准备 conversation，供后续步骤使用。
    conversation = journal.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    # 继续执行前验证内部不变量。
    assert str(UUID(conversation.agent_thread_id)) == conversation.agent_thread_id
    # 准备 turn and user and replay，供后续步骤使用。
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
    # 准备 same_turn and same_user and
    # same_replay，供后续步骤使用。
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

    # 继续执行前验证内部不变量。
    assert not replay
    # 继续执行前验证内部不变量。
    assert same_replay
    # 继续执行前验证内部不变量。
    assert same_turn.turn_id == turn.turn_id
    # 继续执行前验证内部不变量。
    assert same_user.message_id == user.message_id
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
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
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ConversationNotFound):
        journal.get_owned(conversation.conversation_id, "tenant-b", "subject-a")

    # 前置条件满足后调用 bind server run。
    journal.bind_server_run(turn.turn_id, "server-run-1", "running")
    # 准备 assistant，供后续步骤使用。
    assistant = journal.append_assistant_message(run_id=turn.run_id, content="AAPL summary")
    # 继续执行前验证内部不变量。
    assert (
        journal.append_assistant_message(run_id=turn.run_id, content="AAPL summary").message_id
        == assistant.message_id
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ConversationConflict):
        journal.append_assistant_message(run_id=turn.run_id, content="conflicting replacement")
    # 准备 branch，供后续步骤使用。
    branch = journal.append_branch_message(
        run_id=turn.run_id,
        content="Regenerated AAPL summary",
        parent_message_id=assistant.message_id,
    )
    # 准备 messages，供后续步骤使用。
    messages = journal.list_messages(conversation.conversation_id)
    # 继续执行前验证内部不变量。
    assert [item.sequence for item in messages] == [1, 2, 3]
    # 继续执行前验证内部不变量。
    assert branch.parent_message_id == assistant.message_id
    # 继续执行前验证内部不变量。
    assert messages[0].content == "Compare AAPL with MSFT"
    # 前置条件满足后调用 close。
    database.close()


def test_journal_survives_database_and_process_reconstruction(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 path，供后续步骤使用。
    path = tmp_path / "restart.db"
    # 准备 first_database and first，供后续步骤使用。
    first_database, first = repository(path)
    # 准备 conversation，供后续步骤使用。
    conversation = first.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    # 准备 turn and _ and _，供后续步骤使用。
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
    # 前置条件满足后调用 bind server run。
    first.bind_server_run(turn.turn_id, "server-restart", "pending")
    # 前置条件满足后调用 close。
    first_database.close()

    # 准备 second_database and second，供后续步骤使用。
    second_database, second = repository(path)
    # 准备 restored，供后续步骤使用。
    restored = second.get_owned(conversation.conversation_id, "tenant-a", "subject-a")
    # 准备 restored_turn，供后续步骤使用。
    restored_turn = second.get_turn_owned(turn.run_id, "tenant-a", "subject-a")
    # 继续执行前验证内部不变量。
    assert restored.agent_thread_id == conversation.agent_thread_id
    # 继续执行前验证内部不变量。
    assert restored.agent_profile_version == "1.0.0"
    # 继续执行前验证内部不变量。
    assert restored_turn.server_run_id == "server-restart"
    # 继续执行前验证内部不变量。
    assert second.list_messages(conversation.conversation_id)[0].content.startswith("Remember")
    # 前置条件满足后调用 close。
    second_database.close()
