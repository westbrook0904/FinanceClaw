"""Real HTTP memory management, source admission and native-job isolation."""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from financeclaw.api.application.memory_service import MemoryManagementService
from financeclaw.api.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.api.http.errors import install_error_handlers
from financeclaw.api.http.memory import memory_router
from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.repository import MemoryRepository
from financeclaw.shared.memory.tables import MemorySourceRow
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from tests.stage10.conftest import admit as admit
from tests.stage10.conftest import service as service


@pytest.fixture
def memory_app(service):
    """Mount the exact product memory routes with real SQL and two independent owners."""
    principals = {
        name: AuthenticatedPrincipal(
            tenant_id="tenant" if name != "foreign" else "other",
            subject_id="user",
            scopes={"memory:read"}
            if name == "reader"
            else {"memory:read", "memory:write", "memory:delete"},
        )
        for name in ("owner", "reader", "foreign")
    }
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        memory_router(
            MemoryManagementService(service.sessions, service.settings),
            StaticBearerAuthenticator(principals),
        )
    )
    return app


@pytest.mark.asyncio
async def test_http_candidate_confirm_update_forget_and_permanent_replay(memory_app, service):
    """Keep decisions independent of Turns and prevent old receipts from resurrecting facts."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=memory_app),
        base_url="http://test",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        payload = {"kind": "profile", "field": "investment_goal", "content": "三年后购房"}
        response = await client.post(
            "/v1/memories", json=payload, headers={"Idempotency-Key": "goal"}
        )
        assert response.status_code == 201, response.text
        proposal = response.json()
        assert proposal["status"] == "proposed"
        candidate = (await client.get("/v1/memories/" + proposal["candidate_id"])).json()
        decision = {
            "decision": "approve",
            "expected_revision": candidate["revision"],
            "content_hash": candidate["content_hash"],
        }
        path = "/v1/memory/candidates/" + candidate["memory_id"] + "/decision"
        assert (
            await client.post(
                path,
                json=decision,
                headers={"Authorization": "Bearer reader", "Idempotency-Key": "accept"},
            )
        ).status_code == 403
        assert (
            await client.get(
                "/v1/memories/" + candidate["memory_id"],
                headers={"Authorization": "Bearer foreign"},
            )
        ).status_code == 404
        approved = await client.post(path, json=decision, headers={"Idempotency-Key": "accept"})
        assert approved.status_code == 200, approved.text
        fact = approved.json()
        assert fact["status"] == "committed"
        deleted = await client.request(
            "DELETE",
            "/v1/memories/" + fact["memory_id"],
            json={"expected_revision": fact["revision"]},
            headers={"Idempotency-Key": "forget"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "forgotten"
        replay = await client.post(path, json=decision, headers={"Idempotency-Key": "accept"})
        assert replay.status_code == 200 and replay.json()["replayed"]
        assert (await client.get("/v1/memories/" + fact["memory_id"])).status_code == 404
        assert (await client.get("/v1/memories")).json()["items"] == []
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 0
        assert session.scalar(select(func.count()).select_from(TurnCommandRow)) == 0


@pytest.mark.asyncio
async def test_http_settings_and_validation_do_not_accept_ownership(memory_app):
    """Require current permission and revision; reject caller-provided ownership metadata."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=memory_app),
        base_url="http://test",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        current = (await client.get("/v1/memory/settings")).json()
        body = {"expected_revision": current["policy_revision"], "auto_extract": False}
        response = await client.patch(
            "/v1/memory/settings", json=body, headers={"Idempotency-Key": "disable"}
        )
        assert response.status_code == 200 and not response.json()["auto_enabled"]
        assert (
            await client.patch(
                "/v1/memory/settings", json=body, headers={"Idempotency-Key": "stale"}
            )
        ).status_code == 409
        for extra in ("tenant_id", "subject_id", "approved", "explicit_intent", "evidence"):
            response = await client.post(
                "/v1/memories",
                headers={"Idempotency-Key": "bad"},
                json={"kind": "task", "content": "一次已完成的研究", extra: "injected"},
            )
            assert response.status_code == 422


def test_admission_commits_exact_preference_without_model_or_native_run(service, admit):
    """S02: trusted persistent low-risk evidence commits with admission, before model execution."""
    accepted = admit(message="以后都用中文回答，简短一些")
    actor = MemoryActor(tenant_id="tenant", subject_id="user", scopes={"memory:read"})
    profiles = MemoryRepository(service.sessions).list_records(actor, kind="profile")
    assert any(row.field == "language" and row.content == "zh-CN" for row in profiles)
    with service.sessions() as session:
        source = session.scalar(
            select(MemorySourceRow).where(MemorySourceRow.turn_id == accepted.turn_id)
        )
        assert source.permit and source.source_kind == "user_message"
        assert not list(
            session.scalars(
                select(OutboxEventRow).where(OutboxEventRow.destination == "memory_extract")
            )
        )


@pytest.mark.asyncio
async def test_explicit_turn_revoke_cancels_background_permit(service, admit):
    """S37: explicit revocation blocks outstanding derivation without waiting for job pickup."""
    accepted = admit(message="分析债券市场")
    await service.revoke_authorization(
        accepted.turn_id, tenant_id="tenant", subject_id="user", scopes={"*"}, command_id="revoke"
    )
    with service.sessions() as session:
        assert session.scalar(
            select(MemorySourceRow).where(MemorySourceRow.turn_id == accepted.turn_id)
        ).permit_revoked
