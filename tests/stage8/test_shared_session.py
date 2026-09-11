"""共享 Session 的基本回归；真实 PostgreSQL flush 故障与竞争由实验执行器覆盖。"""

import pytest
from sqlalchemy import func, select

from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase


def test_admission_writes_do_not_commit_callers_transaction(tmp_path) -> None:
    """已执行的 Journal、快照和操作 SQL 随最外层事务一同回滚。"""
    db = ApplicationDatabase(f"sqlite:///{tmp_path / 'shared.db'}")
    db.initialize_schema()
    repository = SqlAlchemyConversationRepository(db.session_factory)
    conversation = repository.create_conversation(
        tenant_id="tenant",
        subject_id="owner",
        agent_id="finance_agent",
        agent_profile_version="1.6.0",
    )
    with pytest.raises(RuntimeError, match="crash"):
        with db.session_factory.begin() as session:
            turn, _, _ = repository.begin_turn(
                conversation_id=conversation.conversation_id,
                tenant_id="tenant",
                subject_id="owner",
                idempotency_key="key",
                request_hash="a" * 64,
                message="hello",
                target_type="agent",
                target_id="finance_agent",
                target_version="1.6.0",
                session=session,
            )
            repository.execution.register(turn.run_id, {"frozen": True}, session=session)
            repository.execution.prepare("start", turn.run_id, {"input": "hello"}, session=session)
            assert repository.execution.claim("start", session=session)
            session.flush()
            raise RuntimeError("crash after SQL, before commit")
    with db.session_factory() as session:
        for table in (
            ConversationTurnRow,
            ConversationMessageRow,
            RunExecutionRow,
            RunOperationRow,
        ):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    db.close()


def test_execution_root_cannot_be_replaced_by_idempotent_registration(tmp_path) -> None:
    """幂等登记复用原根，但不能替换已冻结的执行快照。"""
    db = ApplicationDatabase(f"sqlite:///{tmp_path / 'root.db'}")
    db.initialize_schema()
    execution = SqlAlchemyConversationRepository(db.session_factory).execution
    execution.register("root", {"input": "frozen"})
    assert execution.register("root", {"input": "frozen"})["root_run_id"] == "root"
    with pytest.raises(ExecutionConflict, match="root"):
        execution.register("root", {"input": "changed"})
    db.close()


@pytest.mark.parametrize("first_write", ["snapshot", "operation"])
def test_first_savepoint_cannot_commit_an_outer_sqlite_transaction(tmp_path, first_write) -> None:
    """没有前置 Journal UPDATE 时，SQLite 的首次 SAVEPOINT 也必须服从外层回滚。"""
    db = ApplicationDatabase(f"sqlite:///{tmp_path / 'savepoint.db'}")
    db.initialize_schema()
    execution = SqlAlchemyConversationRepository(db.session_factory).execution
    if first_write == "operation":
        execution.register("root", {"input": "frozen"})
    with pytest.raises(RuntimeError, match="crash"):
        with db.session_factory.begin() as session:
            if first_write == "snapshot":
                execution.register("root", {"input": "frozen"}, session=session)
            else:
                execution.prepare("start", "root", {"input": "frozen"}, session=session)
            raise RuntimeError("crash after SAVEPOINT release")
    with db.session_factory() as session:
        table = RunExecutionRow if first_write == "snapshot" else RunOperationRow
        assert session.scalar(select(func.count()).select_from(table)) == 0
    db.close()
