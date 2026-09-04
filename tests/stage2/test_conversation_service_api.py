"""`test_conversation_service_api` 模块提供`stage2`相关能力。"""

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from financeclaw.application import (
    ConversationService,
    RunService,
    ServerRun,
    TargetResolver,
)
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import ApplicationDatabase, FinanceClawSettings
from financeclaw.interfaces.http import create_app
from financeclaw.interfaces.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.kernel import ConversationTurnRequest
from financeclaw.modules.conversation import SqlAlchemyConversationRepository


class FakeAgentServerClient:
    """`FakeAgentServerClient` 封装外部服务的调用边界。"""

    def __init__(self) -> None:
        """初始化 `FakeAgentServerClient` 及其必需的协作对象。"""
        self.threads: list[str] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []

    async def create_thread(self, thread_id: str) -> None:
        """创建 `thread`，并返回边界约定的结果。"""
        self.threads.append(thread_id)

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        """创建 `run`，并返回边界约定的结果。"""
        # 准备 run_id，供后续步骤使用。
        run_id = f"server-{len(self.runs) + 1}"
        # 准备 call，供后续步骤使用。
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "context": context,
            "metadata": metadata,
        }
        # 前置条件满足后调用 append。
        self.create_calls.append(call)
        # 准备 working state，供后续步骤使用。
        self.runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "status": "pending",
            "metadata": metadata,
            "output": {
                "messages": [AIMessage(content=f"answer from {run_id}")],
            },
        }
        # 向调用方返回符合边界约定的结果。
        return ServerRun(run_id=run_id, status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """获取 `run`，并返回边界约定的结果。"""
        del thread_id
        return self.runs[run_id]

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """等待并合并 `run`，并返回边界约定的结果。"""
        del thread_id
        return self.runs[run_id]["output"]

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """查找 `run`，并返回边界约定的结果。"""
        for run_id, run in self.runs.items():
            metadata = run.get("metadata", {})
            if (
                run.get("thread_id") == thread_id
                and metadata.get("application_run_id") == application_run_id
            ):
                return ServerRun(run_id=run_id, status=str(run["status"]))
        return None

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        """恢复 `run`，并返回边界约定的结果。"""
        del thread_id, assistant_id, command, context, metadata
        return {"messages": [AIMessage(content="approved answer")]}

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        """以流式方式输出 `FakeAgentServerClient`，并返回边界约定的结果。"""
        yield {"event": "values", "data": {"status": "running"}}

    def stream_run(self, *, thread_id: str, run_id: str) -> AsyncIterator[Any]:
        """以流式方式输出指定 `run`，并返回边界约定的结果。"""
        del thread_id, run_id
        return self._stream()

    async def health(self) -> bool:
        """检查健康状态 `FakeAgentServerClient`，并返回边界约定的结果。"""
        return True


def build_persistent_components(path: Path):
    """构建 `persistent_components`，并返回边界约定的结果。"""
    return build_components(
        FinanceClawSettings(
            environment="test",
            offline_model=True,
            debug_full_io=False,
            database_url=SecretStr(f"sqlite+pysqlite:///{path}"),
            artifact_root=str(path.parent / "artifacts"),
        ),
        enable_persistence=True,
    )


@pytest.mark.asyncio
async def test_restart_reconciliation_thread_mapping_profile_pin_and_ownership(
    tmp_path: Path,
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database_path，供后续步骤使用。
    database_path = tmp_path / "restart.db"
    # 准备 components，供后续步骤使用。
    components = build_persistent_components(database_path)
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 继续执行前验证内部不变量。
    assert isinstance(repository, SqlAlchemyConversationRepository)
    # 准备 fake，供后续步骤使用。
    fake = FakeAgentServerClient()
    # 准备 service，供后续步骤使用。
    service = ConversationService(
        fake,
        repository,
        components.agent_profiles,
        summary_service=components.summary_service,
    )
    # 准备 conversation，供后续步骤使用。
    conversation = await service.create(tenant_id="tenant-a", subject_id="subject-a")
    # 准备 accepted，供后续步骤使用。
    accepted = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="first question"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="first-turn",
    )
    # 准备 replay，供后续步骤使用。
    replay = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="first question"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="first-turn",
    )
    # 继续执行前验证内部不变量。
    assert replay.run_id == accepted.run_id
    # 继续执行前验证内部不变量。
    assert replay.idempotent_replay
    # 继续执行前验证内部不变量。
    assert len(fake.create_calls) == 1
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()

    # 准备 working state，供后续步骤使用。
    fake.runs["server-1"]["status"] = "success"
    # 准备 restarted_database，供后续步骤使用。
    restarted_database = ApplicationDatabase(f"sqlite+pysqlite:///{database_path}")
    # 准备 restarted_repository，供后续步骤使用。
    restarted_repository = SqlAlchemyConversationRepository(restarted_database.session_factory)
    # 准备 restarted，供后续步骤使用。
    restarted = ConversationService(
        fake,
        restarted_repository,
        components.agent_profiles,
    )
    # 继续执行前验证内部不变量。
    assert await restarted.reconcile_incomplete() == (accepted.run_id,)
    # 准备 persisted，供后续步骤使用。
    persisted = restarted.messages(
        conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
    )
    # 继续执行前验证内部不变量。
    assert [item.role for item in persisted.messages] == ["user", "assistant"]
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(LookupError):
        restarted.get(
            conversation.conversation_id,
            tenant_id="tenant-b",
            subject_id="subject-b",
        )
    # 准备 second，供后续步骤使用。
    second = await restarted.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="second question"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="second-turn",
    )
    # 继续执行前验证内部不变量。
    assert second.thread_id == accepted.thread_id
    # 继续执行前验证内部不变量。
    assert fake.create_calls[-1]["thread_id"] == accepted.thread_id
    # 继续执行前验证内部不变量。
    assert fake.create_calls[-1]["context"]["conversation_id"] == conversation.conversation_id
    # 前置条件满足后调用 close。
    restarted_database.close()


@pytest.mark.asyncio
async def test_conversation_http_contract_and_cross_tenant_isolation(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = build_persistent_components(tmp_path / "api.db")
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 继续执行前验证内部不变量。
    assert isinstance(repository, SqlAlchemyConversationRepository)
    # 准备 fake，供后续步骤使用。
    fake = FakeAgentServerClient()
    # 准备 conversation_service，供后续步骤使用。
    conversation_service = ConversationService(fake, repository, components.agent_profiles)
    # 准备 run_service，供后续步骤使用。
    run_service = RunService(
        fake,
        TargetResolver(
            tool_catalog=components.tool_catalog,
            agent_profiles=components.agent_profiles,
        ),
    )
    # 准备 authenticator，供后续步骤使用。
    authenticator = StaticBearerAuthenticator(
        {
            "token-a": AuthenticatedPrincipal(
                tenant_id="tenant-a",
                subject_id="subject-a",
                scopes={"market:read"},
            ),
            "token-b": AuthenticatedPrincipal(
                tenant_id="tenant-b",
                subject_id="subject-b",
                scopes={"market:read"},
            ),
        }
    )
    # 准备 app，供后续步骤使用。
    app = create_app(
        run_service=run_service,
        authenticator=authenticator,
        conversation_service=conversation_service,
    )
    # 准备 transport，供后续步骤使用。
    transport = httpx.ASGITransport(app=app)
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations", json={}, headers={"Authorization": "Bearer token-a"}
        )
        rejected_agent_selection = await client.post(
            "/v1/conversations",
            json={"agent_id": "specialist"},
            headers={"Authorization": "Bearer token-a"},
        )
        conversation_id = created.json()["conversation_id"]
        started = await client.post(
            f"/v1/conversations/{conversation_id}/turns",
            json={"message": "read AAPL"},
            headers={
                "Authorization": "Bearer token-a",
                "Idempotency-Key": "api-turn",
            },
        )
        rejected_target = await client.post(
            f"/v1/conversations/{conversation_id}/turns",
            json={
                "message": "switch Agent",
                "target": {"kind": "agent", "agent_id": "specialist"},
            },
            headers={
                "Authorization": "Bearer token-a",
                "Idempotency-Key": "forbidden-target",
            },
        )
        blocked_control_plane = await client.post(
            "/v1/runs",
            json={"message": "bypass root Agent"},
            headers={
                "Authorization": "Bearer token-a",
                "Idempotency-Key": "blocked-control-plane",
            },
        )
        hidden = await client.get(
            f"/v1/conversations/{conversation_id}",
            headers={"Authorization": "Bearer token-b"},
        )
        messages = await client.get(
            f"/v1/conversations/{conversation_id}/messages",
            headers={"Authorization": "Bearer token-a"},
        )
        health = await client.get("/health")
        openapi = (await client.get("/openapi.json")).json()

    # 继续执行前验证内部不变量。
    assert created.status_code == 201
    # 继续执行前验证内部不变量。
    assert "agent_id" not in created.json()
    # 继续执行前验证内部不变量。
    assert "agent_profile_version" not in created.json()
    # 继续执行前验证内部不变量。
    assert rejected_agent_selection.status_code == 422
    # 继续执行前验证内部不变量。
    assert started.status_code == 202
    # 继续执行前验证内部不变量。
    assert started.json()["conversation_id"] == conversation_id
    # 继续执行前验证内部不变量。
    assert "target_kind" not in started.json()
    # 继续执行前验证内部不变量。
    assert "thread_id" not in started.json()
    # 继续执行前验证内部不变量。
    assert rejected_target.status_code == 422
    # 继续执行前验证内部不变量。
    assert blocked_control_plane.status_code == 403
    # 继续执行前验证内部不变量。
    assert hidden.status_code == 404
    # 继续执行前验证内部不变量。
    assert [item["role"] for item in messages.json()["messages"]] == ["user"]
    # 继续执行前验证内部不变量。
    assert health.json()["stage"] == "6"
    # 继续执行前验证内部不变量。
    assert "/v1/conversations/{conversation_id}/turns" in openapi["paths"]
    # 继续执行前验证内部不变量。
    assert "/v1/tools/{tool_id}:invoke" not in openapi["paths"]
    # 继续执行前验证内部不变量。
    assert "/v1/workflows/{workflow_id}/runs" not in openapi["paths"]
    # 继续执行前验证内部不变量。
    assert "/v1/runs" not in openapi["paths"]
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()


@pytest.mark.asyncio
async def test_replay_recovers_server_run_created_before_local_bind(
    tmp_path: Path, monkeypatch
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = build_persistent_components(tmp_path / "bind-recovery.db")
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 继续执行前验证内部不变量。
    assert isinstance(repository, SqlAlchemyConversationRepository)
    # 准备 fake，供后续步骤使用。
    fake = FakeAgentServerClient()
    # 准备 service，供后续步骤使用。
    service = ConversationService(fake, repository, components.agent_profiles)
    # 准备 conversation，供后续步骤使用。
    conversation = await service.create(tenant_id="tenant-a", subject_id="subject-a")
    # 准备 request，供后续步骤使用。
    request = ConversationTurnRequest(message="recover this dispatch")
    # 准备 original_bind，供后续步骤使用。
    original_bind = repository.bind_server_run
    # 准备 attempts，供后续步骤使用。
    attempts = 0

    # 定义当前边界使用的 fail first bind 处理器。
    def fail_first_bind(turn_id: str, server_run_id: str, status: str):
        """处理 `first_bind`，并返回边界约定的结果。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated local commit failure")
        return original_bind(turn_id, server_run_id, status)

    # 前置条件满足后调用 setattr。
    monkeypatch.setattr(repository, "bind_server_run", fail_first_bind)
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(RuntimeError, match="local commit failure"):
        await service.start_turn(
            conversation.conversation_id,
            request,
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=frozenset({"market:read"}),
            idempotency_key="bind-window",
        )
    # 准备 recovered，供后续步骤使用。
    recovered = await service.start_turn(
        conversation.conversation_id,
        request,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="bind-window",
    )

    # 继续执行前验证内部不变量。
    assert recovered.idempotent_replay
    # 继续执行前验证内部不变量。
    assert recovered.status == "pending"
    # 继续执行前验证内部不变量。
    assert len(fake.create_calls) == 1
    # 继续执行前验证内部不变量。
    assert attempts == 2
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()
