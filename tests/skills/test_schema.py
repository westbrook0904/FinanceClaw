"""Skills 旧库缺列、就绪阻断与 PostgreSQL 保留历史数据的修复回归。"""

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select

from financeclaw.api.bootstrap import create_default_app
from financeclaw.memory_worker.bootstrap import require_schema as require_memory_schema
from financeclaw.shared.conversation.models import ModelContextManifest
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.conversation.tables import ConversationMessageRow, ModelContextManifestRow
from financeclaw.shared.infrastructure.orm import Base
from financeclaw.shared.notifications.facts import require_schema as require_notification_schema
from financeclaw.shared.turns.repository import TurnRepository
from tests.stage3.support import conversation_context
from tests.stage10.runtime import runtime as runtime
from tests.stage11.test_worker_postgres import postgres_database as postgres_database

SKILL_COLUMNS = (
    ("conversation_messages", "skill_access_refs"),
    ("model_context_manifests", "skill_catalog_hash"),
    ("model_context_manifests", "skill_catalog_omitted"),
    ("model_context_manifests", "skill_refs"),
    ("model_context_manifests", "skill_resource_refs"),
    ("model_context_manifests", "skill_access_refs"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "column"), SKILL_COLUMNS)
async def test_missing_skill_column_blocks_startup_and_readiness(
    runtime, monkeypatch, table, column
):
    """每个新增列缺失都必须在受理前被发现，不能等到提交或模型记录写入时报错。"""
    app = create_default_app(runtime.turns.settings, client=runtime.client)
    app.state.resources, app.state.turns = runtime.resources, runtime.turns
    monkeypatch.setattr(runtime.turns.lifecycle, "healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(runtime.turns.events, "healthy", AsyncMock(return_value=True))
    with runtime.resources.database.engine.begin() as connection:
        connection.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    with pytest.raises(RuntimeError, match="schema is required"):
        runtime.turns.store.require_schema()
    if table == "conversation_messages":
        with pytest.raises(RuntimeError, match="schema is required"):
            require_memory_schema(runtime.resources.database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/health/ready")
        assert response.status_code == 503 and response.json() == {"ready": False}


def manifest_for(context, **changes):
    """使用真实历史任务建立最小模型清单，不调用模型或外部渠道。"""
    return ModelContextManifest(
        manifest_id=str(uuid4()),
        model_call_id=str(uuid4()),
        conversation_id=context.conversation_id,
        turn_id=context.turn_id,
        prompt_template_version="1.0.0",
        agent_profile_version="1.8.0",
        model_profile_version="1.0.0",
        input_token_count=10,
        available_input_tokens=1000,
        context_hash="a" * 64,
        **changes,
    )


def apply_repair(database):
    """在测试独占 schema 中执行与现场修复相同的事务 SQL。"""
    source = Path("deploy/postgres/skills-runtime.sql").read_text()
    with database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(source)


def test_postgres_repair_preserves_history_and_supports_skill_writes(postgres_database):
    """旧行回填、结构一致、新来源读写与重复执行均用独立真实 PostgreSQL schema 验证。"""
    database = postgres_database
    repository = SqlAlchemyConversationRepository(database.session_factory)
    context, message_id = conversation_context(repository, message="修复前保留的历史消息")
    original = repository.save_manifest(manifest_for(context))
    with database.engine.begin() as connection:
        for table, column in SKILL_COLUMNS:
            connection.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        connection.exec_driver_sql(
            "ALTER TABLE notification_targets ALTER COLUMN turn_id SET NOT NULL"
        )
    with pytest.raises(RuntimeError):
        TurnRepository(database.session_factory).require_schema()
    apply_repair(database)
    TurnRepository(database.session_factory).require_schema()
    require_memory_schema(database)
    require_notification_schema(database.session_factory)
    with database.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    with database.session_factory() as session:
        old_message = session.get(ConversationMessageRow, message_id)
        old_manifest = session.get(ModelContextManifestRow, original.manifest_id)
        assert old_message.content == "修复前保留的历史消息" and old_message.skill_access_refs == []
        assert old_manifest.skill_catalog_hash is None and old_manifest.skill_catalog_omitted == 0
        assert old_manifest.skill_refs == old_manifest.skill_resource_refs == []
        assert old_manifest.skill_access_refs == []
    ref = {"skill_id": "market-brief", "version": "1.0.0", "package_hash": "b" * 64}
    access = {
        "ref": ref,
        "policy_hash": "c" * 64,
        "source_turn_id": context.turn_id,
        "source_scope": "root",
    }
    repository.save_manifest(
        manifest_for(
            context, skill_catalog_hash="d" * 64, skill_refs=(ref,), skill_access_refs=(access,)
        )
    )
    answer = repository.append_assistant_message(turn_id=context.turn_id, content="修复后测试结果")
    assert answer.skill_access_refs[0]["ref"] == ref
    apply_repair(database)
    with database.session_factory() as session:
        assert session.get(ConversationMessageRow, answer.message_id).skill_access_refs == list(
            answer.skill_access_refs
        )
        assert len(list(session.scalars(select(ModelContextManifestRow)))) == 2
