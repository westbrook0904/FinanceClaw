"""8C 正式增量迁移、只读旧根盘点和非破坏性回滚门禁。"""

import os
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from financeclaw.coordination.deployment import DeploymentControl
from financeclaw.coordination.repository import CoordinatorRepository
from financeclaw.shared.execution_ledger.coordination_tables import CoordinatedRunRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from tests.stage8.test_coordinator import admit


def test_incremental_migration_does_not_adopt_and_fenced_schema_cannot_downgrade(
    tmp_path, monkeypatch
):
    """真实 Alembic 从 8B 扩表但不开接管；一旦切换过必须保留部署事实。"""
    admin = os.environ.get("FINANCECLAW_STAGE8_TEST_POSTGRES_URL")
    if admin:
        from experiments.stage8.run import isolated_database

        url = isolated_database(admin)
    else:
        url = f"sqlite:///{tmp_path / 'cutover-migration.db'}"
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    config = Config("alembic.ini")
    database = ApplicationDatabase(url)
    try:
        command.upgrade(config, "0010_stage8b")
        store = CoordinatorRepository(
            database.session_factory, backend_instance_id="langgraph-primary"
        )
        with pytest.raises(RuntimeError, match="Stage-8C"):
            store.require_schema()
        command.upgrade(config, "head")
        store.require_schema()
        state = DeploymentControl(store).view()
        assert state["revision"] == 0 and not state["legacy_fenced"]
        with database.session_factory() as session:
            assert session.scalar(select(CoordinatedRunRow)) is None
        command.downgrade(config, "0010_stage8b")
        command.upgrade(config, "head")
        DeploymentControl(store).change(0, admission_paused=True, dispatch_paused=True)
        with pytest.raises(RuntimeError, match="cutover evidence must be retained"):
            command.downgrade(config, "0010_stage8b")
        assert inspect(database.engine).has_table("coordination_control")
    finally:
        database.close()
        if admin:
            import psycopg
            from psycopg import sql

            with psycopg.connect(admin, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(urlsplit(url).path.lstrip("/"))
                    )
                )


@pytest.mark.asyncio
async def test_new_version_three_root_alone_prevents_schema_downgrade(setup, monkeypatch):
    """没有迁移旧根也不能删掉新根所需门闩；回滚保留兼容 Worker。"""
    await admit(setup)
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", setup.settings.database_url.get_secret_value())
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    config = Config("alembic.ini")
    command.stamp(config, "head")
    with pytest.raises(RuntimeError, match="cutover evidence must be retained"):
        command.downgrade(config, "0010_stage8b")
    setup.store.require_schema()
