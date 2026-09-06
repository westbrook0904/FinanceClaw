"""部署数据库验收入口：仅在提供专用 PostgreSQL 测试连接时创建隔离临时 schema。"""

import asyncio
import os
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from financeclaw.application.execution_service import agent_snapshot
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.database import normalize_database_url
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.execution import ExecutionConflict
from tests.stage6fix.test_execution_recovery import OWNER

pytestmark = pytest.mark.skipif(
    not os.environ.get("FINANCECLAW_TEST_POSTGRES_URL"),
    reason="requires a dedicated PostgreSQL test database",
)


@pytest.mark.asyncio
async def test_postgres_turn_journal_operation_and_budget_cas(tmp_path):
    """在真实 PostgreSQL 上并发争用同一事实；临时 schema 完成后删除，不动现有表。"""
    url = make_url(normalize_database_url(os.environ["FINANCECLAW_TEST_POSTGRES_URL"]))
    assert url.get_backend_name() == "postgresql"
    schema = "stage6fix_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = url.update_query_dict({"options": f"-csearch_path={schema}"})
    components = None
    try:
        components = build_components(
            FinanceClawSettings(
                _env_file=None,
                environment="test",
                offline_model=True,
                database_url=SecretStr(isolated.render_as_string(hide_password=False)),
                artifact_root=str(tmp_path / "artifacts"),
            ),
            enable_persistence=True,
        )
        repository = components.conversation_repository
        conversation = repository.create_conversation(
            **OWNER, agent_id="finance_agent", agent_profile_version="1.1.0"
        )
        requests = await asyncio.gather(
            *(
                asyncio.to_thread(
                    repository.begin_turn,
                    **OWNER,
                    conversation_id=conversation.conversation_id,
                    idempotency_key="same",
                    request_hash="a" * 64,
                    message="bounded test",
                    target_type="agent",
                    target_id="finance_agent",
                    target_version="1.1.0",
                )
                for _ in range(20)
            )
        )
        assert sum(not item[2] for item in requests) == 1
        turn = requests[0][0]
        context = ExecutionContext(
            **OWNER,
            run_id=turn.run_id,
            root_run_id=turn.run_id,
            turn_id=turn.turn_id,
            conversation_id=conversation.conversation_id,
        )
        snapshot = agent_snapshot(
            components.default_agent_profile,
            context,
            thread_id=conversation.agent_thread_id,
            input_hash="a" * 64,
        )
        snapshot["limits"]["tool"] = 7
        repository.execution.register(turn.run_id, snapshot)
        repository.execution.prepare("pg-operation", turn.run_id, {"input": {"message": "test"}})
        claimed = await asyncio.gather(
            *(asyncio.to_thread(repository.execution.claim, "pg-operation") for _ in range(20))
        )
        assert sum(claimed) == 1

        async def consume():
            """并发计数上限不能超卖，预算失败不回退已消费计数。"""
            try:
                await asyncio.to_thread(repository.execution.consume, turn.run_id, "tool")
                return 1
            except ExecutionConflict:
                return 0

        assert sum(await asyncio.gather(*(consume() for _ in range(20)))) == 7
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    repository.append_assistant_message, run_id=turn.run_id, content="finished"
                )
                for _ in range(20)
            )
        )
        assert [
            message.sequence for message in repository.list_messages(conversation.conversation_id)
        ] == [1, 2]
    finally:
        if components is not None:
            components.database.close()
        # 仅清除此测试创建的随机 schema；不接受用户提供的 schema 名或通配符。
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
