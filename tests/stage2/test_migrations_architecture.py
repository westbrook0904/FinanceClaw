"""`test_migrations_architecture` 模块提供`stage2`相关能力。"""

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
    """验证函数名所描述的业务场景符合预期。"""
    root = Path(__file__).parents[2]
    forbidden = (
        "harness_context",
        "ContextItem",
        "ContextSnapshot",
        "ContextProjection",
        "ContextConsumer",
    )
    sources = sorted((root / "financeclaw").rglob("*.py"))
    offenders = {
        str(path.relative_to(root)): term
        for path in sources
        for term in forbidden
        if term in path.read_text()
    }
    assert offenders == {}
    assert '"harness_context"' not in (root / "pyproject.toml").read_text()


def test_alembic_upgrade_downgrade_and_reupgrade(tmp_path: Path, monkeypatch) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database_path，供后续步骤使用。
    database_path = tmp_path / "migrations.db"
    # 准备 url，供后续步骤使用。
    url = f"sqlite+pysqlite:///{database_path}"
    # 前置条件满足后调用 setenv。
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    # 前置条件满足后调用 setenv。
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    # 准备 config，供后续步骤使用。
    config = Config("alembic.ini")

    # 前置条件满足后调用 upgrade。
    command.upgrade(config, "head")
    # 准备 engine，供后续步骤使用。
    engine = create_engine(url)
    # 后续阶段可以扩展应用 Schema；Stage-2 只要求自己负责的表集合存在，不限制新增领域表。
    assert STAGE2_TABLES.issubset(inspect(engine).get_table_names())

    # 前置条件满足后调用 downgrade。
    command.downgrade(config, "base")
    # 继续执行前验证内部不变量。
    assert inspect(engine).get_table_names() == ["alembic_version"]

    # 前置条件满足后调用 upgrade。
    command.upgrade(config, "head")
    # 继续执行前验证内部不变量。
    assert STAGE2_TABLES.issubset(inspect(engine).get_table_names())
    # 前置条件满足后调用 dispose。
    engine.dispose()


def test_database_normalizes_psycopg_driver_and_enforces_sqlite_foreign_keys(
    tmp_path: Path,
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    assert (
        normalize_database_url("postgresql://user:password@example.invalid/app")
        == "postgresql+psycopg://user:password@example.invalid/app"
    )
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'foreign-keys.db'}")
    with database.engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    database.close()
