"""HF-2 release, migration, admission gate, human approval and HTTP capability edges."""

import asyncio
import subprocess
import sys
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from financeclaw.bff.application.conversation_service import ConversationService
from financeclaw.bff.http.app import create_app
from financeclaw.bff.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.bff.http.webhooks import webhook_router
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.root_repository import now
from financeclaw.shared.execution_ledger.run_tables import (
    RootRunRow,
)
from tests.stage8_hotfix.test_bff_runs import (
    OWNER,
    SCOPES,
    admit,
    final_state,
    response,
    tick,
    waiting_state,
)
from tests.stage8_hotfix.test_bff_runs import runtime as runtime


@pytest.mark.asyncio
async def test_stop_before_dispatch_never_starts_remote_run(runtime):
    """启动后的 BFF 直接受理，先到的停止请求封闭尚未发送的操作。"""
    accepted = await admit(runtime)
    await runtime.runs.cancel(accepted.run_id, **OWNER)
    await tick(runtime)
    assert not runtime.client.runs.calls
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "cancelled"


@pytest.mark.asyncio
async def test_expired_answer_and_replay_cannot_create_new_resume(runtime):
    """Expiration is advanced by the observer and GET leaves revision unchanged."""
    accepted = await admit(runtime)
    await tick(runtime)
    waiting_state(runtime, accepted)
    await tick(runtime)
    status = await runtime.runs.status(accepted.run_id, **OWNER)
    item = status.pending_interactions[0]
    with runtime.runs.store.sessions.begin() as session:
        session.get(PendingInteractionRow, item["interaction_id"]).expires_at = now() - timedelta(
            seconds=1
        )
    with pytest.raises(ExecutionConflict):
        await response(runtime, accepted)
    await tick(runtime)
    assert (
        await runtime.runs.status(accepted.run_id, **OWNER)
    ).waiting_reason == "interaction_expired"
    assert len(runtime.client.runs.calls) == 1


@pytest.mark.asyncio
async def test_hitl_approval_scope_action_and_reject_intent(runtime):
    """Reject intent must not deny the composite wrapper before native resume processes it."""
    accepted = await admit(runtime)
    await tick(runtime)
    state = final_state(runtime, accepted)
    call = {
        "id": "write-call",
        "name": "watchlist_add",
        "args": {"symbol": "AAPL", "note": "synthetic"},
    }
    state["values"]["messages"][-1] = {"type": "ai", "content": "", "tool_calls": [call]}
    state["next"] = ["tools"]
    state["tasks"] = [
        {
            "interrupts": [
                {
                    "id": "hitl-1",
                    "value": {
                        "action_requests": [
                            {
                                "name": call["name"],
                                "args": call["args"],
                                "description": "never parsed",
                            }
                        ],
                        "review_configs": [
                            {
                                "action_name": call["name"],
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    },
                }
            ]
        }
    ]
    await tick(runtime)
    item = (await runtime.runs.status(accepted.run_id, **OWNER)).pending_interactions[0]
    answer = InteractionResponse(
        revision=item["revision"],
        kind="approval",
        decision="reject",
        action_hash=item["action_hash"],
    )
    with pytest.raises(PermissionError):
        await runtime.runs.interactions.respond(
            item["interaction_id"],
            answer,
            **OWNER,
            scopes=SCOPES - {"tools:approve"},
            idempotency_key="reject",
        )
    with pytest.raises(ExecutionConflict):
        await runtime.runs.interactions.respond(
            item["interaction_id"],
            answer.model_copy(update={"action_hash": "a" * 64}),
            **OWNER,
            scopes=SCOPES,
            idempotency_key="reject",
        )
    await runtime.runs.interactions.respond(
        item["interaction_id"], answer, **OWNER, scopes=SCOPES, idempotency_key="reject"
    )
    assert not runtime.runs.execution.get(accepted.run_id)["side_effects_denied"]
    await tick(runtime)
    assert len(runtime.client.runs.calls) == 2
    command = runtime.client.runs.calls[-1][2]["command"]["resume"]["hitl-1"]
    assert command == {"decisions": [{"type": "reject"}]}
    assert not runtime.runs.execution.get(accepted.run_id)["side_effects_denied"]


@pytest.mark.asyncio
async def test_response_race_and_changed_native_wait(runtime):
    """Two clients share one decision; a moved checkpoint cannot receive a stale answer."""
    accepted = await admit(runtime)
    await tick(runtime)
    state = waiting_state(runtime, accepted)
    await tick(runtime)
    results = await asyncio.gather(
        response(runtime, accepted, key="first"),
        response(runtime, accepted, key="second"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ExecutionConflict) for value in results) == 1
    state["tasks"][0]["interrupts"][0]["id"] = "changed"
    await tick(runtime)
    assert len(runtime.client.runs.calls) == 1


@pytest.mark.asyncio
async def test_http_new_targets_blocked_and_get_sse_read_only(runtime):
    """Internal callers cannot bypass the top-level root by submitting a Tool/Worker target."""
    principal = AuthenticatedPrincipal(**OWNER, scopes=SCOPES | {"internal:invoke"})
    app = create_app(
        run_service=runtime.runs,
        authenticator=StaticBearerAuthenticator({"test": principal}),
        conversation_service=ConversationService(
            runtime.runs.repository, runtime.releases.agent_profiles, runs=runtime.runs
        ),
        readiness_checks={"agent_server": runtime.client.health},
    )
    accepted = await admit(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        for target in (
            {"kind": "agent", "agent_id": "market_research_agent"},
            {"kind": "tool", "tool_id": "calculate"},
        ):
            r = await client.post(
                "/v1/runs",
                json={
                    "conversation_id": accepted.conversation_id,
                    "message": "x",
                    "target": target,
                },
                headers={"Idempotency-Key": "bypass"},
            )
            assert r.status_code == 404
        for path in (
            "/v1/tools/calculate/invoke",
            "/v1/workflows/portfolio_review/runs",
            f"/v1/runs/{accepted.run_id}/resume",
        ):
            assert (await client.post(path, json={})).status_code == 404
        assert (
            await client.post(
                f"/v1/conversations/{accepted.conversation_id}/turns",
                json={"message": "x", "target": {"kind": "tool", "tool_id": "calculate"}},
                headers={"Idempotency-Key": "invalid-target"},
            )
        ).status_code == 422
        assert (await client.get(f"/v1/runs/{accepted.run_id}")).status_code == 200
    assert not runtime.client.runs.calls
    await runtime.runs.cancel(accepted.run_id, **OWNER)
    events = [
        item
        async for item in runtime.runs.stream(accepted.run_id, **OWNER, last_event_id="invalid")
    ]
    assert events[-1].data["reset_reason"] == "invalid_cursor"
    assert not runtime.client.runs.calls


@pytest.mark.asyncio
async def test_webhook_limits_and_persistence_failure(runtime, monkeypatch):
    """Malformed bodies and failed persistence never receive a success acknowledgement."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(webhook_router(runtime.runs.store, runtime.resources.settings))
    path = "/internal/webhooks/langgraph/langgraph-main"
    headers = {"Authorization": "Bearer synthetic-bff-webhook-token-at-least-32"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.post(path, headers=headers, content=b"x" * 65537)).status_code == 413
        assert (await client.post(path, headers=headers, content=b"bad")).status_code == 422
        assert (
            await client.post(path, headers={**headers, "Content-Encoding": "gzip"}, content=b"x")
        ).status_code == 415

        def fail(_):
            """Inject a durable inbox failure."""
            raise RuntimeError("synthetic database down")

        monkeypatch.setattr(runtime.runs.store, "notify", fail)
        assert (
            await client.post(
                path,
                headers=headers,
                json={"thread_id": "thread", "run_id": "run", "status": "success"},
            )
        ).status_code == 503


def test_worker_manifest_is_identical_across_process_hash_seeds(tmp_path):
    """Serialize governance sets before JSON conversion so independent services pin one release."""
    from experiments.stage8_hotfix.environment import isolated_environment

    code = """from pathlib import Path
from tests.stage8_hotfix.test_bff_runs import config
from financeclaw.shared.releases.catalog import build_release_catalogs
from financeclaw.shared.execution_ledger.repository import digest
print(digest(build_release_catalogs(config(Path('/tmp')),enable_persistence=True).agent_profiles.resolve('finance_agent','1.5.0').model_dump(mode='json')))
"""
    values = []
    for seed in ("1", "2", "3"):
        env = {**isolated_environment(tmp_path / "events.jsonl"), "PYTHONHASHSEED": seed}
        values.append(
            subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip()
        )
    assert len(set(values)) == 1


@pytest.mark.asyncio
async def test_bff_completion_reuses_atomic_notification_and_independent_sender(runtime):
    """Use the real notification ledger with a synthetic gateway, never an actual Feishu send."""
    from types import SimpleNamespace

    from financeclaw.bff.application.feishu_channel_service import (
        FeishuChannelService,
        FeishuInboundMessage,
    )
    from financeclaw.bff.notifications.repository import NotificationRepository
    from financeclaw.bff.notifications.worker import deliver
    from financeclaw.shared.notifications.tables import (
        NotificationDeliveryRow,
        NotificationEventRow,
        NotificationTargetRow,
    )
    from tests.stage8.test_notifications import Gateway, NoDisplay

    conversations = ConversationService(
        runtime.runs.repository, runtime.releases.agent_profiles, runs=runtime.runs
    )
    channel = FeishuChannelService(
        conversations,
        app_id="synthetic-app",
        allowed_open_ids=frozenset({"synthetic-user"}),
        scopes=SCOPES,
    )
    inbound = FeishuInboundMessage(
        message_id="synthetic-message",
        tenant_key="synthetic-tenant",
        sender_open_id="synthetic-user",
        chat_id="synthetic-chat",
        chat_type="p2p",
        content_type="text",
        text="HF2 synthetic channel turn",
        sender_type="user",
    )
    assert await channel.process(inbound, NoDisplay()) == "accepted"
    with runtime.runs.store.sessions() as session:
        target = session.scalar(select(NotificationTargetRow))
        root = session.get(RootRunRow, target.run_id)
        accepted = SimpleNamespace(
            run_id=root.run_id,
            thread_id=root.projection["thread_id"],
            conversation_id=root.conversation_id,
        )
    await tick(runtime)
    final_state(runtime, accepted)
    await tick(runtime)
    await tick(runtime)
    with runtime.runs.store.sessions() as session:
        events = list(
            session.scalars(
                select(NotificationEventRow).where(NotificationEventRow.kind == "terminal")
            )
        )
        assert len(events) == 1 and events[0].payload["content"] == "verified final answer"
    sender = NotificationRepository(
        runtime.runs.store.sessions,
        app_id="synthetic-app",
        allowed_open_ids=frozenset({"synthetic-user"}),
    )
    while sender.materialize():
        pass
    gateway = Gateway()
    while claim := sender.claim("synthetic-sender", lease_seconds=30):
        await deliver(sender, gateway, claim, runtime.runs.settings)
    with runtime.runs.store.sessions() as session:
        delivery = session.scalar(select(NotificationDeliveryRow))
        assert delivery.status == "sent"
    assert len(gateway.calls) == 2 and len(runtime.client.runs.calls) == 1
