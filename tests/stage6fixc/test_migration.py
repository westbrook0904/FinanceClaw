"""C 阶段增量迁移、单待交互约束与不可丢弃事实的回滚检查。"""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.execution import ExecutionRepository
from financeclaw.modules.interactions import InteractionRepository


@pytest.mark.parametrize("with_facts", [False, True])
def test_c_migration_only_downgrades_when_no_interaction_facts(tmp_path, monkeypatch, with_facts):
    """空 C 升级可回滚；保存过问题即必须显式归档，不破坏历史决定和恢复证据。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0007_stage6fix_ab")
    command.upgrade(config, "head")
    database = ApplicationDatabase(url)
    try:
        indexes = inspect(database.engine).get_indexes("pending_interactions")
        assert any(i["name"] == "uq_interactions_pending_root" and i["unique"] for i in indexes)
        if with_facts:
            execution = ExecutionRepository(database.session_factory)
            context = ExecutionContext(
                tenant_id="tenant", subject_id="subject", run_id="root", turn_id="turn"
            )
            execution.register(
                "root", {"context": context.model_dump(mode="json"), "thread_id": "t"}
            )
            execution.prepare("start", "root", {})
            execution.bind("start", "server")
            interactions = InteractionRepository(execution)
            now = datetime.now(UTC)
            row = interactions.register(
                "root",
                source="agent_declared",
                server_run_id="server",
                interrupt_id="native",
                point_id="details",
                kind="input",
                question="问题",
                request={},
                expires_at=now + timedelta(minutes=15),
                now=now,
            )
            with pytest.raises(RuntimeError, match="archival migration"):
                command.downgrade(config, "0007_stage6fix_ab")
            assert (
                interactions.get_owned(row["interaction_id"], "tenant", "subject", now=now)[
                    "status"
                ]
                == "pending"
            )
        else:
            command.downgrade(config, "0007_stage6fix_ab")
            assert "pending_interactions" not in inspect(database.engine).get_table_names()
        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0008_stage6fix_c" if with_facts else "0007_stage6fix_ab"
            )
    finally:
        database.close()
