"""Canonical product routes reject native coordinates and never advance execution on reads."""

import httpx
import pytest

from financeclaw.api.application.conversation_service import ConversationService
from financeclaw.api.http.app import create_app
from financeclaw.api.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from tests.stage10.runtime import OWNER, SCOPES, admit
from tests.stage10.runtime import runtime as runtime


@pytest.mark.asyncio
async def test_product_http_contract_is_scoped_and_message_only(runtime):
    """Reject native coordinates and foreign conversation references from product requests."""
    service = runtime.turns
    authenticator = StaticBearerAuthenticator(
        {
            "owner": AuthenticatedPrincipal(**OWNER, scopes=SCOPES),
            "foreign": AuthenticatedPrincipal(
                tenant_id="foreign", subject_id="foreign", scopes=SCOPES
            ),
        }
    )
    app = create_app(
        turns=service,
        conversations=ConversationService(service.journal, service.releases.agents, turns=service),
        authenticator=authenticator,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        response = await client.post("/v1/conversations", json={})
        assert response.status_code == 201
        conversation_id = response.json()["conversation_id"]
        base = f"/v1/conversations/{conversation_id}"
        assert (await client.post(base + "/turns", json={"message": "hi"})).status_code == 422
        for field in ("run_id", "thread_id", "checkpoint", "metadata", "assistant_id", "callback"):
            response = await client.post(
                base + "/turns",
                headers={"Idempotency-Key": "one"},
                json={"message": "hi", field: "untrusted"},
            )
            assert response.status_code == 422
        response = await client.post(
            base + "/turns", headers={"Idempotency-Key": "one"}, json={"message": "hi"}
        )
        assert response.status_code == 202
        accepted = response.json()
        assert not {"run_id", "native_run_id", "thread_id", "command_id"} & accepted.keys()
        path = base + "/turns/" + accepted["turn_id"]
        assert (await client.get(path)).json()["status"] == "accepted"
        assert (
            await client.get(path, headers={"Authorization": "Bearer foreign"})
        ).status_code == 404
        assert (await client.get(path.replace(conversation_id, "another"))).status_code == 404
        assert (await client.get(base + "/messages?limit=1")).status_code == 200
        assert (await client.get("/v1/runs/" + accepted["turn_id"])).status_code == 404
        assert not runtime.client.calls


@pytest.mark.asyncio
async def test_explicit_controls_replay_without_extending_authorization(runtime):
    """Reuse channel control receipts after the current authorization revision has advanced."""
    accepted = await admit(runtime)
    before = await runtime.turns.status(accepted.turn_id, **OWNER)
    await runtime.turns.reauthorize(
        accepted.turn_id, **OWNER, scopes=SCOPES, command_id="channel-event"
    )
    first = runtime.turns.execution.get(accepted.turn_id)
    await runtime.turns.reauthorize(
        accepted.turn_id, **OWNER, scopes=SCOPES, command_id="channel-event"
    )
    second = runtime.turns.execution.get(accepted.turn_id)
    assert first["grant_revision"] == before.authorization_revision + 1
    assert second["grant_revision"] == first["grant_revision"]
    assert second["grant_expires_at"] == first["grant_expires_at"]
