"""Current empty-database schema and admission-control concurrency guarantees."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from financeclaw.bff.application.runs.store import BFFRunRepository
from financeclaw.bff.run_control import BFFDeploymentControl
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
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
            assert connection.execute(
                text("SELECT revision, bff_admission_enabled, bff_dispatch_paused FROM run_control")
            ).one() == (0, False, False)
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


def test_control_revision_has_one_winner_and_preserves_unspecified_fields(tmp_path):
    """Two deployers cannot both apply the same revision, including on SQLite."""
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'controls.db'}")
    database.initialize_schema()
    control = BFFDeploymentControl(
        BFFRunRepository(database.session_factory, backend_instance_id="test")
    )
    barrier = Barrier(2)

    def configure():
        """Race two independent sessions against the same expected revision."""
        barrier.wait(timeout=5)
        try:
            return control.configure(0, admission_enabled=True)
        except ExecutionConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: configure(), range(2)))
        assert results.count("conflict") == 1
        assert control.view()["revision"] == 1
        assert control.view()["bff_admission_enabled"] is True
        result = control.configure(1, dispatch_paused=True)
        assert result["bff_admission_enabled"] is True
        assert result["bff_dispatch_paused"] is True
        assert result["revision"] == 2
    finally:
        database.close()
