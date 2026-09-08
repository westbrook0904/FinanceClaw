"""SSE 独立游标、保留期外快照恢复和通知 API 的纯读归属保护。"""

import asyncio

import httpx
import pytest
from sqlalchemy import delete, func, select

from financeclaw.bff.http.app import create_app
from financeclaw.bff.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.shared.execution_ledger.coordination_tables import RunProgressEventRow
from tests.stage8.test_notifications import completed


def application(setup):
    """生产路由配合测试身份，后台仍使用正式受理 Facade。"""
    return create_app(
        run_service=setup.services.runs,
        conversation_service=setup.bff,
        authenticator=StaticBearerAuthenticator(
            {
                "owner": AuthenticatedPrincipal(
                    tenant_id="feishu:tenant", subject_id="feishu:user", scopes=setup.scopes
                ),
                "other": AuthenticatedPrincipal(
                    tenant_id="other", subject_id="other", scopes=setup.scopes
                ),
            }
        ),
    )


@pytest.mark.asyncio
async def test_independent_cursor_replay_snapshot_and_readonly_queries(setup):
    """多观察者从自己的游标回放，SSE 最终答案来自 Journal，没有新的通知或执行。"""
    _, _, run_id, repository = await completed(setup)
    with setup.store.sessions() as session:
        before = session.scalar(select(func.count()).select_from(RunProgressEventRow))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(setup)),
        base_url="http://bff",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        results = await asyncio.gather(
            *[
                client.get(
                    f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": f"{run_id}:{cursor}"}
                )
                for cursor in [0, 1, 1, 999]
            ]
        )
        assert all(
            response.status_code == 200 and "final answer" in response.text for response in results
        )
        assert results[1].text == results[2].text
        assert "future_cursor" in results[-1].text
        assert f"id: {run_id}:" in results[0].text
        assert '"replay": true' in results[0].text
        state = await client.get(f"/v1/runs/{run_id}/notifications")
        assert state.json()["unmaterialized_events"] == 1
        assert (
            await client.get(
                f"/v1/runs/{run_id}/notifications", headers={"Authorization": "Bearer other"}
            )
        ).status_code == 404
        assert (
            await client.get(f"/v1/runs/{run_id}/events", headers={"Authorization": "Bearer other"})
        ).status_code == 404
    assert setup.backend.calls == setup.backend.reads == 1
    with setup.store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RunProgressEventRow)) == before
    assert repository.materialize()


@pytest.mark.asyncio
async def test_expired_history_and_invalid_cursor_reset_to_safe_snapshot(setup):
    """事件历史回收后明确重置，不丢终态答案，不从别的根游标读取事件。"""
    _, _, run_id, _ = await completed(setup)
    with setup.store.sessions.begin() as session:
        session.execute(delete(RunProgressEventRow).where(RunProgressEventRow.run_id == run_id))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(setup)),
        base_url="http://bff",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        for cursor, reason in [
            (f"{run_id}:0", "history_unavailable"),
            ("other-root:1", "invalid_cursor"),
            ("bad", "invalid_cursor"),
            (f"{run_id}:" + "9" * 180, "invalid_cursor"),
        ]:
            response = await client.get(
                f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": cursor}
            )
            assert response.status_code == 200 and reason in response.text
            assert "assistant.completed" in response.text and "final answer" in response.text
        assert (await client.delete(f"/v1/runs/{run_id}/notifications")).json()["active"] is False
        assert (
            await client.delete(
                f"/v1/runs/{run_id}/notifications", headers={"Authorization": "Bearer other"}
            )
        ).status_code == 404
