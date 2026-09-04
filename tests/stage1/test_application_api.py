"""`test_application_api` 模块提供`stage1`相关能力。"""

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from financeclaw.application import RunService, ServerRun, TargetResolver
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.interfaces.http import create_app
from financeclaw.interfaces.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator


class FakeAgentServerClient:
    """`FakeAgentServerClient` 封装外部服务的调用边界。"""

    def __init__(self) -> None:
        """初始化 `FakeAgentServerClient` 及其必需的协作对象。"""
        self.threads: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.resume_commands: list[dict[str, Any]] = []

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
        # 前置条件满足后调用 append。
        self.runs.append(
            {
                "thread_id": thread_id,
                "assistant_id": assistant_id,
                "input": input,
                "context": context,
                "metadata": metadata,
            }
        )
        # 向调用方返回符合边界约定的结果。
        return ServerRun(run_id=f"server-{len(self.runs)}", status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """获取 `run`，并返回边界约定的结果。"""
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
        """恢复 `run`，并返回边界约定的结果。"""
        del thread_id, assistant_id, context, metadata
        self.resume_commands.append(command)
        return {"response": {"status": "success"}}

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


def build_api():
    """构建 `api`，并返回边界约定的结果。"""
    # 准备 settings，供后续步骤使用。
    settings = FinanceClawSettings(environment="test", offline_model=True, debug_full_io=False)
    # 准备 components，供后续步骤使用。
    components = build_components(settings)
    # 准备 fake，供后续步骤使用。
    fake = FakeAgentServerClient()
    # 准备 service，供后续步骤使用。
    service = RunService(
        fake,
        TargetResolver(
            tool_catalog=components.tool_catalog,
            agent_profiles=components.agent_profiles,
        ),
    )
    # 准备 auth，供后续步骤使用。
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
    # 向调用方返回符合边界约定的结果。
    return create_app(run_service=service, authenticator=auth), fake


@pytest.mark.asyncio
async def test_internal_default_agent_dispatch_and_bff_idempotency() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 app and fake，供后续步骤使用。
    app, fake = build_api()
    # 准备 transport，供后续步骤使用。
    transport = httpx.ASGITransport(app=app)
    # 准备 headers，供后续步骤使用。
    headers = {"Authorization": "Bearer token-a", "Idempotency-Key": "direct-key"}
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
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

    # 继续执行前验证内部不变量。
    assert unauthorized.status_code == 401
    # 继续执行前验证内部不变量。
    assert response.status_code == 202
    # 继续执行前验证内部不变量。
    assert fake.runs[0]["assistant_id"] == "direct_tool"
    # 继续执行前验证内部不变量。
    assert fake.runs[0]["input"]["tool_id"] == "market_snapshot"
    # 继续执行前验证内部不变量。
    assert hidden.status_code == 404
    # 继续执行前验证内部不变量。
    assert status.json()["status"] == "success"
    # 继续执行前验证内部不变量。
    assert resumed.json()["status"] == "completed"
    # 继续执行前验证内部不变量。
    assert fake.resume_commands == [{"resume": {"decisions": [{"type": "approve"}]}}]
    # 继续执行前验证内部不变量。
    assert "event: run.progress" in stream.text
    # 继续执行前验证内部不变量。
    assert ready.status_code == 200
    # 继续执行前验证内部不变量。
    assert ready.headers["x-request-id"]
