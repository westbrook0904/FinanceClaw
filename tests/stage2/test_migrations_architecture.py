from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from financeclaw.infrastructure import ApplicationDatabase, normalize_database_url

STAGE2_TABLES = {
    "alembic_version",
    "artifacts",
    "conversation_messages",
    "conversation_summaries",
    "conversation_turns",
    "conversations",
    "model_context_manifests",
}


def test_stage2_runtime_does_not_reference_deleted_context_stack() -> None:
    root = Path(__file__).parents[2]
    forbidden = (
        "harness_context",
        "ContextItem",
        "ContextSnapshot",
        "ContextProjection",
        "ContextConsumer",
    )
    sources = [
        *sorted((root / "financeclaw").rglob("*.py")),
        *sorted((root / "harness-contracts" / "src").rglob("*.py")),
    ]
    offenders = {
        str(path.relative_to(root)): term
        for path in sources
        for term in forbidden
        if term in path.read_text()
    }
    assert offenders == {}
    assert '"harness_context"' not in (root / "pyproject.toml").read_text()


def test_alembic_upgrade_downgrade_and_reupgrade(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "migrations.db"
    url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    engine = create_engine(url)
    # Later stages may extend the application schema; Stage 2 owns and
    # continues to require this subset rather than forbidding new domain rows.
    assert STAGE2_TABLES.issubset(inspect(engine).get_table_names())

    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]

    command.upgrade(config, "head")
    assert STAGE2_TABLES.issubset(inspect(engine).get_table_names())
    engine.dispose()


def test_database_normalizes_psycopg_driver_and_enforces_sqlite_foreign_keys(
    tmp_path: Path,
) -> None:
    assert (
        normalize_database_url("postgresql://user:password@example.invalid/app")
        == "postgresql+psycopg://user:password@example.invalid/app"
    )
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'foreign-keys.db'}")
    with database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    database.close()
