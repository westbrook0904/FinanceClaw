"""8B 增量迁移快照与非破坏性回滚门禁。"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.notifications.facts import require_schema
from financeclaw.shared.notifications.tables import NotificationSenderRow


def test_notification_migration_and_evidence_preservation(tmp_path, monkeypatch):
    """缺迁移拒绝启动；真实迁移产生完整列和约束，有证据不能 downgrade。"""
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    config = Config("alembic.ini")
    command.upgrade(config, "0009_stage8a")
    database = ApplicationDatabase(url)
    try:
        with pytest.raises(RuntimeError, match="Stage-8B"):
            require_schema(database.session_factory)
        command.upgrade(config, "head")
        require_schema(database.session_factory)
        inspector = inspect(database.engine)
        assert any(
            index["unique"] and index["name"] == "uq_notification_delivery_part"
            for index in inspector.get_indexes("notification_deliveries")
        )
        command.downgrade(config, "0009_stage8a")
        command.upgrade(config, "head")
        with database.session_factory.begin() as session:
            session.add(NotificationSenderRow(worker_id="synthetic", app_id="app"))
        with pytest.raises(RuntimeError, match="evidence must be retained"):
            command.downgrade(config, "0009_stage8a")
        require_schema(database.session_factory)
    finally:
        database.close()
