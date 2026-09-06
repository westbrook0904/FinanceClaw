"""A/B 事实表的真实迁移与回滚保护，不用 create_all 代替发布路径。"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.modules.execution import ExecutionRepository


def test_migration_preserves_execution_facts_on_downgrade(tmp_path, monkeypatch) -> None:
    """有授权／操作事实的库不能通过旧版 downgrade 静默丢弃恢复证据。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'migration-safety.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    database = ApplicationDatabase(url)
    try:
        assert {"run_executions", "run_operations"}.issubset(
            inspect(database.engine).get_table_names()
        )
        indexes = inspect(database.engine).get_indexes("conversation_messages")
        assert any(
            index["name"] == "uq_messages_final_assistant" and index["unique"] for index in indexes
        )
        repository = ExecutionRepository(database.session_factory)
        repository.register("migration-run", {"purpose": "preserve execution facts"})
        repository.prepare("migration-operation", "migration-run", {"command": "start"})
        with pytest.raises(RuntimeError, match="archival migration"):
            command.downgrade(config, "0006_stage6")
        assert repository.operation("migration-operation")["status"] == "prepared"
        with database.engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0007_stage6fix_ab"
            )
    finally:
        database.close()
