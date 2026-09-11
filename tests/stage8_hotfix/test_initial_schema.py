"""当前空库迁移、完整列结构与根执行约束。"""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.orm import Base


def test_initial_schema_matches_runtime_and_rejects_child_executions(tmp_path, monkeypatch):
    """A fresh upgrade matches every runtime table; child executions fail at the SQL boundary."""
    url = f"sqlite+pysqlite:///{tmp_path / 'initial.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    config = Config("alembic.ini")
    assert [item.revision for item in ScriptDirectory.from_config(config).walk_revisions()] == [
        "0001_initial"
    ]
    command.upgrade(config, "head")
    database = ApplicationDatabase(url)
    try:
        with database.engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            assert set(inspect(connection).get_table_names()) == {
                *Base.metadata.tables,
                "alembic_version",
            }
            assert len(Base.metadata.tables) == 19
            assert "backend_attempts" not in Base.metadata.tables
            assert "run_control" not in Base.metadata.tables
        with pytest.raises(IntegrityError, match="ck_execution_is_root"):
            with database.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO run_executions "
                        "(run_id, root_run_id, snapshot, cancellation_requested, "
                        "cancellation_confirmed, side_effects_denied, model_calls, "
                        "tool_calls, operation_calls, created_at) VALUES "
                        "('child', 'root', '{}', false, false, false, 0, 0, 0, CURRENT_TIMESTAMP)"
                    )
                )
        command.downgrade(config, "base")
        assert inspect(database.engine).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        with database.engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        database.close()
