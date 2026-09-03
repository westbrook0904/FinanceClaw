from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from financeclaw.api import create_app
from financeclaw.api.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.application import (
    ConversationService,
    RunService,
    ServerRun,
    TargetResolver,
)
from financeclaw.bootstrap import build_components
from financeclaw.contracts import AgentTarget, RunRequest, ToolTarget
from financeclaw.conversation import ConversationConflict, SqlAlchemyConversationRepository
from financeclaw.infrastructure import ApplicationDatabase, FinanceClawSettings


class FakeAgentServerClient:
    def __init__(self) -> None:
        self.threads: list[str] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []

    async def create_thread(self, thread_id: str) -> None:
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
        run_id = f"server-{len(self.runs) + 1}"
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "context": context,
            "metadata": metadata,
        }
        self.create_calls.append(call)
        self.runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "status": "pending",
            "metadata": metadata,
            "output": {
                "messages": [AIMessage(content=f"answer from {run_id}")],
            },
        }
        return ServerRun(run_id=run_id, status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        del thread_id
        return self.runs[run_id]

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        del thread_id
        return self.runs[run_id]["output"]

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
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
        del thread_id, assistant_id, command, context, metadata
        return {"messages": [AIMessage(content="approved answer")]}

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        yield {"event": "values", "data": {"status": "running"}}

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        del thread_id, assistant_id
        return self._stream()

    async def health(self) -> bool:
        return True


def build_persistent_components(path: Path):
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
    database_path = tmp_path / "restart.db"
    components = build_persistent_components(database_path)
    repository = components.conversation_repository
    assert isinstance(repository, SqlAlchemyConversationRepository)
    fake = FakeAgentServerClient()
    service = ConversationService(
        fake,
        repository,
        components.agent_profiles,
        summary_service=components.summary_service,
    )
    conversation = await service.create(tenant_id="tenant-a", subject_id="subject-a")
    accepted = await service.start_turn(
        RunRequest(conversation_id=conversation.conversation_id, message="first question"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="first-turn",
    )
    replay = await service.start_turn(
        RunRequest(conversation_id=conversation.conversation_id, message="first question"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="first-turn",
    )
    assert replay.run_id == accepted.run_id
    assert replay.idempotent_replay
    assert len(fake.create_calls) == 1
    with pytest.raises(ConversationConflict):
        await service.start_turn(
            RunRequest(
                conversation_id=conversation.conversation_id,
                message="switch target",
                target=ToolTarget(tool_id="market_snapshot", arguments={"symbol": "AAPL"}),
            ),
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=frozenset({"market:read"}),
            idempotency_key="wrong-target",
        )
    with pytest.raises(ConversationConflict):
        await service.start_turn(
            RunRequest(
                conversation_id=conversation.conversation_id,
                message="switch profile",
                target=AgentTarget(agent_id="finance_agent", version="9.9.9"),
            ),
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=frozenset({"market:read"}),
            idempotency_key="wrong-profile",
        )
    if components.database is not None:
        components.database.close()

    fake.runs["server-1"]["status"] = "success"
    restarted_database = ApplicationDatabase(f"sqlite+pysqlite:///{database_path}")
    restarted_repository = SqlAlchemyConversationRepository(restarted_database.session_factory)
    restarted = ConversationService(
        fake,
        restarted_repository,
        components.agent_profiles,
    )
    assert await restarted.reconcile_incomplete() == (accepted.run_id,)
    persisted = restarted.messages(
        conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
    )
    assert [item.role for item in persisted.messages] == ["user", "assistant"]
    with pytest.raises(LookupError):
        restarted.get(
            conversation.conversation_id,
            tenant_id="tenant-b",
            subject_id="subject-b",
        )
    second = await restarted.start_turn(
        RunRequest(conversation_id=conversation.conversation_id, message="second question"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="second-turn",
    )
    assert second.thread_id == accepted.thread_id
    assert fake.create_calls[-1]["thread_id"] == accepted.thread_id
    assert fake.create_calls[-1]["context"]["conversation_id"] == conversation.conversation_id
    restarted_database.close()


@pytest.mark.asyncio
async def test_conversation_http_contract_and_cross_tenant_isolation(tmp_path: Path) -> None:
    components = build_persistent_components(tmp_path / "api.db")
    repository = components.conversation_repository
    assert isinstance(repository, SqlAlchemyConversationRepository)
    fake = FakeAgentServerClient()
    conversation_service = ConversationService(fake, repository, components.agent_profiles)
    run_service = RunService(
        fake,
        TargetResolver(
            tool_catalog=components.tool_catalog,
            agent_profiles=components.agent_profiles,
        ),
    )
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
    app = create_app(
        run_service=run_service,
        authenticator=authenticator,
        conversation_service=conversation_service,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations", json={}, headers={"Authorization": "Bearer token-a"}
        )
        conversation_id = created.json()["conversation_id"]
        started = await client.post(
            "/v1/runs",
            json={"conversation_id": conversation_id, "message": "read AAPL"},
            headers={
                "Authorization": "Bearer token-a",
                "Idempotency-Key": "api-turn",
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

    assert created.status_code == 201
    assert started.status_code == 202
    assert started.json()["conversation_id"] == conversation_id
    assert hidden.status_code == 404
    assert [item["role"] for item in messages.json()["messages"]] == ["user"]
    assert health.json()["stage"] == "3"
    if components.database is not None:
        components.database.close()


@pytest.mark.asyncio
async def test_replay_recovers_server_run_created_before_local_bind(
    tmp_path: Path, monkeypatch
) -> None:
    components = build_persistent_components(tmp_path / "bind-recovery.db")
    repository = components.conversation_repository
    assert isinstance(repository, SqlAlchemyConversationRepository)
    fake = FakeAgentServerClient()
    service = ConversationService(fake, repository, components.agent_profiles)
    conversation = await service.create(tenant_id="tenant-a", subject_id="subject-a")
    request = RunRequest(
        conversation_id=conversation.conversation_id,
        message="recover this dispatch",
    )
    original_bind = repository.bind_server_run
    attempts = 0

    def fail_first_bind(turn_id: str, server_run_id: str, status: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated local commit failure")
        return original_bind(turn_id, server_run_id, status)

    monkeypatch.setattr(repository, "bind_server_run", fail_first_bind)
    with pytest.raises(RuntimeError, match="local commit failure"):
        await service.start_turn(
            request,
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=frozenset({"market:read"}),
            idempotency_key="bind-window",
        )
    recovered = await service.start_turn(
        request,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        idempotency_key="bind-window",
    )

    assert recovered.idempotent_replay
    assert recovered.status == "pending"
    assert len(fake.create_calls) == 1
    assert attempts == 2
    if components.database is not None:
        components.database.close()
