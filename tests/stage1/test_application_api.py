from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from financeclaw.api import create_app
from financeclaw.api.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.application import RunService, ServerRun, TargetResolver
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings


class FakeAgentServerClient:
    def __init__(self) -> None:
        self.threads: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.resume_commands: list[dict[str, Any]] = []

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
        self.runs.append(
            {
                "thread_id": thread_id,
                "assistant_id": assistant_id,
                "input": input,
                "context": context,
                "metadata": metadata,
            }
        )
        return ServerRun(run_id=f"server-{len(self.runs)}", status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        return {"run_id": run_id, "thread_id": thread_id, "status": "success", "output": {}}

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        del thread_id, assistant_id, context, metadata
        self.resume_commands.append(command)
        return {"response": {"status": "success"}}

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        yield {"event": "values", "data": {"status": "running"}}

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        del thread_id, assistant_id
        return self._stream()

    async def health(self) -> bool:
        return True


def build_api():
    settings = FinanceClawSettings(environment="test", offline_model=True, debug_full_io=False)
    components = build_components(settings)
    fake = FakeAgentServerClient()
    service = RunService(
        fake,
        TargetResolver(
            tool_catalog=components.tool_catalog,
            agent_profiles=components.agent_profiles,
        ),
    )
    auth = StaticBearerAuthenticator(
        {
            "token-a": AuthenticatedPrincipal(
                tenant_id="tenant-a",
                subject_id="subject-a",
                scopes={"market:read", "watchlist:write", "internal:invoke"},
            ),
            "token-b": AuthenticatedPrincipal(
                tenant_id="tenant-b",
                subject_id="subject-b",
                scopes={"market:read"},
            ),
        }
    )
    return create_app(run_service=service, authenticator=auth), fake


@pytest.mark.asyncio
async def test_internal_default_agent_dispatch_and_bff_idempotency() -> None:
    app, fake = build_api()
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer token-a", "Idempotency-Key": "same-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/v1/runs", json={"message": "read AAPL"}, headers=headers)
        replay = await client.post("/v1/runs", json={"message": "read AAPL"}, headers=headers)
        conflict = await client.post("/v1/runs", json={"message": "read MSFT"}, headers=headers)

    assert first.status_code == 202
    assert first.json()["target_kind"] == "agent"
    assert not first.json()["idempotent_replay"]
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert replay.json()["idempotent_replay"]
    assert conflict.status_code == 409
    assert len(fake.runs) == 1
    assert fake.runs[0]["assistant_id"] == "finance_agent"
    assert "tenant_id" not in fake.runs[0]["input"]


@pytest.mark.asyncio
async def test_internal_tool_dispatch_auth_ownership_resume_and_stream() -> None:
    app, fake = build_api()
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer token-a", "Idempotency-Key": "direct-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthorized = await client.post(
            "/v1/tools/market_snapshot:invoke",
            json={"arguments": {"symbol": "AAPL"}},
            headers={"Idempotency-Key": "missing-auth"},
        )
        response = await client.post(
            "/v1/tools/market_snapshot:invoke",
            json={"arguments": {"symbol": "AAPL"}},
            headers=headers,
        )
        run_id = response.json()["run_id"]
        hidden = await client.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer token-b"})
        status = await client.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer token-a"})
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"type": "approve"},
            headers={"Authorization": "Bearer token-a"},
        )
        stream = await client.get(
            f"/v1/runs/{run_id}/events",
            headers={"Authorization": "Bearer token-a"},
        )
        ready = await client.get("/ready")

    assert unauthorized.status_code == 401
    assert response.status_code == 202
    assert fake.runs[0]["assistant_id"] == "direct_tool"
    assert fake.runs[0]["input"]["tool_id"] == "market_snapshot"
    assert hidden.status_code == 404
    assert status.json()["status"] == "success"
    assert resumed.json()["status"] == "completed"
    assert fake.resume_commands == [{"resume": {"decisions": [{"type": "approve"}]}}]
    assert "event: values" in stream.text
    assert ready.status_code == 200
    assert ready.headers["x-request-id"]
