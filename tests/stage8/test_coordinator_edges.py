"""Coordinator 的 HTTP 边界、授权、精确交付和事务故障回归。"""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from financeclaw.bff.http.app import create_app
from financeclaw.bff.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.coordination.ingress.app import create_ingress
from financeclaw.coordination.interactions.repository import InteractionConflict
from financeclaw.coordination.repository import StaleCoordinator, aware, now
from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.kernel.coordination import BackendNotification, SubmissionReceipt
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.execution_ledger.coordination_tables import (
    BackendAttemptRow,
    ContinuationRow,
    CoordinatedRunRow,
    CoordinationInboxRow,
    RunAuthorizationRow,
    RunProgressEventRow,
)
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.tables import RunOperationRow
from tests.stage8.test_coordinator import admit, tick


def notification(reference, *, event_id="test-event"):
    """最小通知包含不可解释的尝试 ID；不会携带业务输入。"""
    return BackendNotification(
        backend_instance_id=reference.backend_instance_id,
        execution_id=reference.execution_id,
        event_id=event_id,
        status_hint="success",
        payload_digest=digest(event_id),
        received_at=now(),
    )


@pytest.mark.asyncio
async def test_inbox_early_duplicate_late_and_binding_race(setup):
    """早到和回执交错可补偿；重复与旧通知不会制造新操作或回退完成状态。"""
    _, accepted = await admit(setup)
    await tick(setup)
    reference = next(iter(setup.backend.references.values()))
    event = notification(reference)
    assert setup.store.notify(event)
    assert not setup.store.notify(event)
    with setup.store.sessions.begin() as session:
        row = session.scalar(
            select(CoordinationInboxRow).where(CoordinationInboxRow.kind == "backend_notification")
        )
        row.run_id = None  # 回调读不到尚未提交的回执，后于 bind 提交。
    setup.store.reconcile_inbox()
    with setup.store.sessions() as session:
        assert (
            session.scalar(
                select(CoordinationInboxRow).where(
                    CoordinationInboxRow.kind == "backend_notification"
                )
            ).run_id
            == accepted.run_id
        )
    await tick(setup)
    setup.store.notify(notification(reference, event_id="late"))
    assert setup.store.claim_due("another", lease_seconds=5) is None
    assert setup.backend.calls == 1
    with setup.store.sessions() as session:
        assert session.get(CoordinatedRunRow, accepted.run_id).projection["status"] == "completed"


@pytest.mark.asyncio
async def test_lease_fencing_and_new_wake_are_preserved(setup):
    """失效 epoch 无法写进度／回执；旧 finish 不能覆盖在途新唤醒。"""
    _, accepted = await admit(setup)
    first = setup.store.claim_due("first", lease_seconds=5)
    assert setup.store.claim_due("second", lease_seconds=5) is None
    with setup.store.sessions.begin() as session:
        session.get(CoordinatedRunRow, accepted.run_id).lease_until = now() - timedelta(seconds=1)
    assert not setup.store.renew(first, lease_seconds=5)
    second = setup.store.claim_due("second", lease_seconds=5)
    assert second["epoch"] > first["epoch"]
    with pytest.raises(StaleCoordinator):
        setup.coordinator.blocked(first, "obsolete")
    with setup.store.sessions.begin() as session:
        setup.store.command_inbox(session, setup.store.lock(session, accepted.run_id), "new-wake")
    setup.store.finish(second, delay=300)
    assert setup.store.claim_due("third", lease_seconds=5) is not None


@pytest.mark.asyncio
async def test_grant_revoke_and_reauthorization_preserve_frozen_command(setup):
    """撤销在执行端也生效；原主体续期不重写输入、操作 hash 或请求时钟。"""
    _, accepted = await admit(setup)
    before = setup.store.execution.operations_for_run(accepted.run_id)[0]
    original = setup.store.execution.get(accepted.run_id)["snapshot"]
    context = snapshot_context(original)
    await setup.services.conversations.revoke_authorization(
        accepted.run_id, tenant_id="tenant", subject_id="subject"
    )
    with pytest.raises(ExecutionConflict):
        setup.store.execution.verify_context(context)
    with pytest.raises(ExecutionConflict):
        setup.store.execution.consume(accepted.run_id, "model")
    await tick(setup)
    assert setup.backend.calls == 0
    await setup.services.conversations.reauthorize(
        accepted.run_id, tenant_id="tenant", subject_id="subject", scopes=setup.scopes | {"extra"}
    )
    after = setup.store.execution.operations_for_run(accepted.run_id)[0]
    assert (before["request"], before["request_hash"]) == (after["request"], after["request_hash"])
    assert setup.store.execution.get(accepted.run_id)["snapshot"] == original
    with setup.store.sessions() as session:
        assert "extra" not in session.get(RunAuthorizationRow, accepted.run_id).scopes
    await tick(setup)
    await tick(setup)
    assert setup.backend.calls == 1


@pytest.mark.asyncio
async def test_unknown_submission_cancel_waits_until_receipt_confirmed(setup, monkeypatch):
    """未知提交不能被取消标记掩盖；原回执恢复后才逐项确认整树停止。"""
    setup.backend.lose_receipt = True
    _, accepted = await admit(setup)
    with pytest.raises(ConnectionError):
        await tick(setup)
    lookup = setup.backend.lookup_operation

    async def unknown(_):
        """暂时查不到原操作，不构造远端未执行的结论。"""
        return SubmissionReceipt(status="uncertain")

    monkeypatch.setattr(setup.backend, "lookup_operation", unknown)
    await setup.bff.cancel(accepted.run_id, tenant_id="tenant", subject_id="subject")
    await tick(setup)
    with setup.store.sessions() as session:
        row = session.get(CoordinatedRunRow, accepted.run_id)
        assert row.active and row.projection["waiting_reason"] == "submission_uncertain"
    monkeypatch.setattr(setup.backend, "lookup_operation", lookup)
    for _ in range(3):
        await tick(setup)
    assert setup.backend.calls == setup.backend.cancels == 1
    assert (
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    ).status == "cancelled"


@pytest.mark.asyncio
async def test_result_commit_failure_rolls_back_journal_and_progress(setup, monkeypatch):
    """最终投影故障必须回滚 Journal 和操作终态；重入不会重跑 backend。"""
    conversation, accepted = await admit(setup)
    await tick(setup)
    original = setup.store.project

    def fail(session, row, **changes):
        """在 Journal 之后注入故障。"""
        if changes.get("status") == "completed":
            raise RuntimeError("final commit boundary")
        return original(session, row, **changes)

    monkeypatch.setattr(setup.store, "project", fail)
    with pytest.raises(RuntimeError):
        await tick(setup)
    assert len(setup.store.journal.list_messages(conversation.conversation_id)) == 1
    assert setup.store.execution.operations_for_run(accepted.run_id)[0]["status"] == "submitted"
    monkeypatch.setattr(setup.store, "project", original)
    await tick(setup)
    assert len(setup.store.journal.list_messages(conversation.conversation_id)) == 2
    assert setup.backend.calls == 1


@pytest.mark.asyncio
async def test_decision_binding_replay_and_short_auth_deadline(setup):
    """错误 revision 不能决定；重复回答不扩权续期；短认证限制运行期权限。"""
    setup.backend.delegate = True
    _, accepted = await admit(setup)
    for _ in range(4):
        await tick(setup)
    status = await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    pending = status.pending_interactions[0]
    service = setup.services.conversations.interactions
    arguments = {
        "tenant_id": "tenant",
        "subject_id": "subject",
        "scopes": setup.scopes,
        "idempotency_key": "answer",
    }
    with pytest.raises(InteractionConflict):
        await service.respond(
            pending["interaction_id"],
            InteractionResponse(revision=99, kind="input", answer={"scope": "test"}),
            **arguments,
        )
    response = InteractionResponse(
        revision=pending["revision"], kind="input", answer={"scope": "test"}
    )
    expiry = now() + timedelta(seconds=10)
    auth = AuthorizationEvidence(
        source="oidc",
        source_hash=digest("verified synthetic JWT"),
        issued_at=now(),
        expires_at=expiry,
    )
    await service.respond(pending["interaction_id"], response, authorization=auth, **arguments)
    with setup.store.sessions() as session:
        grant_revision = session.get(RunAuthorizationRow, accepted.run_id).revision
    await service.respond(pending["interaction_id"], response, **arguments)
    with setup.store.sessions() as session:
        grant = session.get(RunAuthorizationRow, accepted.run_id)
        assert grant.revision == grant_revision
        assert aware(grant.expires_at).timestamp() == pytest.approx(expiry.timestamp(), abs=0.01)
    with pytest.raises(InteractionConflict):
        await service.respond(
            pending["interaction_id"],
            response.model_copy(update={"answer": {"scope": "changed"}}),
            **arguments,
        )


@pytest.mark.asyncio
async def test_completion_requires_response_application_evidence(setup, monkeypatch):
    """Child 终态和恢复已应用分别确认，缺证据不能向原 parent 交付。"""
    setup.backend.delegate = True
    _, accepted = await admit(setup)
    for _ in range(4):
        await tick(setup)
    request = (
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    ).pending_interactions[0]
    await setup.services.conversations.interactions.respond(
        request["interaction_id"],
        InteractionResponse(revision=request["revision"], kind="input", answer={}),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="decision",
    )
    await tick(setup)
    observe = setup.backend.observe_execution

    async def unproven(ref):
        """后端只有完成通知，缺少恢复应用证据。"""
        return (await observe(ref)).model_copy(update={"response_applications": ()})

    monkeypatch.setattr(setup.backend, "observe_execution", unproven)
    with pytest.raises(ExecutionConflict, match="application"):
        await tick(setup)
    assert setup.backend.calls == 3
    with setup.store.sessions() as session:
        assert not any(row.applied_operation_id for row in session.scalars(select(ContinuationRow)))


@pytest.mark.asyncio
async def test_ingress_auth_persistence_readiness_and_body_limit(setup, monkeypatch):
    """401 先于 JSON 解析；DB 失败不确认，重启后仍有已确认 Inbox。"""
    app = create_ingress(setup.store, setup.settings)
    headers = {
        "Authorization": "Bearer " + setup.settings.coordinator_webhook_token.get_secret_value()
    }
    path = "/internal/webhooks/langgraph-primary"
    payload = {
        "run_id": "native-run",
        "thread_id": "native-thread",
        "status": "success",
        "values": {"secret": "not retained"},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ingress"
    ) as client:
        assert (await client.post(path, content=b"bad")).status_code == 401
        assert (
            await client.post("/internal/webhooks/wrong", headers=headers, json=payload)
        ).status_code == 401
        assert (await client.post(path, headers=headers, content=b"x" * 65537)).status_code == 413
        assert (await client.get("/ready")).status_code == 503
        setup.store.heartbeat("worker")
        assert (await client.get("/ready")).status_code == 200
        assert (await client.post(path, headers=headers, json=payload)).status_code == 204
        assert (await client.post(path, headers=headers, json=payload)).status_code == 204
        with setup.store.sessions() as session:
            rows = list(session.scalars(select(CoordinationInboxRow)))
            assert len(rows) == 1 and "not retained" not in str(rows[0].payload)

        def fail(_):
            """持久化失败必须向 backend 返回可重试状态。"""
            raise OSError("database unavailable")

        monkeypatch.setattr(setup.store, "notify", fail)
        assert (await client.post(path, headers=headers, json=payload)).status_code == 503


@pytest.mark.asyncio
async def test_bff_http_and_multiple_sse_observers_are_read_only(setup):
    """正式 HTTP 路由受理后，无观察者也完成；GET／SSE 不写执行事实。"""
    app = create_app(
        run_service=setup.services.runs,
        conversation_service=setup.bff,
        authenticator=StaticBearerAuthenticator(
            {
                "user": AuthenticatedPrincipal(
                    tenant_id="tenant",
                    subject_id="subject",
                    scopes=setup.scopes,
                )
            }
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff",
        headers={"Authorization": "Bearer user"},
    ) as client:
        conversation = (await client.post("/v1/conversations", json={})).json()
        path = f"/v1/conversations/{conversation['conversation_id']}/turns"
        response = await client.post(
            path, json={"message": "synthetic"}, headers={"Idempotency-Key": "first"}
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert setup.backend.calls == 0
    await tick(setup)
    await tick(setup)
    with setup.store.sessions() as session:
        before = session.scalar(select(func.count()).select_from(RunProgressEventRow))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff",
        headers={"Authorization": "Bearer user"},
    ) as client:
        reads = await asyncio.gather(*[client.get(f"/v1/runs/{run_id}/events") for _ in range(10)])
        assert all(
            response.status_code == 200 and "assistant.completed" in response.text
            for response in reads
        )
        assert (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "completed"
    assert setup.backend.calls == setup.backend.reads == 1
    with setup.store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RunProgressEventRow)) == before
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 2
        assert session.scalar(select(func.count()).select_from(RunOperationRow)) == 1
        assert session.scalar(select(func.count()).select_from(BackendAttemptRow)) == 1


@pytest.mark.asyncio
async def test_legacy_driver_cannot_touch_coordinated_root(setup):
    """兼容服务在任何远端读取／写入前拒绝协调根，防止索引键被当作原生 ID。"""
    from financeclaw.coordination.execution.service import ExecutionService

    _, accepted = await admit(setup)
    legacy = ExecutionService(setup.services.client, setup.store.execution)
    for call in (
        legacy.reconcile(accepted.run_id),
        legacy.result(accepted.run_id, "start"),
        legacy.confirm_tree_stopped(accepted.run_id),
    ):
        with pytest.raises(ExecutionConflict, match="exclusively"):
            await call
    assert setup.backend.calls == 0


def claim_in_process(database_url, barrier, queue):
    """独立连接和地址空间竞争同一到期根及业务命令，获胜者保留租约。"""
    from financeclaw.coordination.repository import CoordinatorRepository
    from financeclaw.shared.infrastructure.database import ApplicationDatabase

    database = ApplicationDatabase(database_url)
    store = CoordinatorRepository(database.session_factory, backend_instance_id="langgraph-primary")
    try:
        barrier.wait(timeout=20)
        claim = store.claim_due("process-" + str(__import__("os").getpid()), lease_seconds=30)
        claimed = False
        if claim:
            operation = store.execution.operations_for_run(claim["run_id"])[0]
            claimed = store.claim_operation(claim, operation["operation_id"])
        queue.put(claimed)
    finally:
        database.close()


@pytest.mark.asyncio
async def test_postgres_four_processes_claim_one_operation_and_budget(setup):
    """真实 PostgreSQL 的进程竞争不能重复领取命令或消费预算。"""
    import multiprocessing

    if not setup.settings.database_url.get_secret_value().startswith("postgresql"):
        pytest.skip("explicit isolated PostgreSQL cluster required")
    _, accepted = await admit(setup)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(4), context.Queue()
    children = [
        context.Process(
            target=claim_in_process,
            args=(setup.settings.database_url.get_secret_value(), barrier, queue),
        )
        for _ in range(4)
    ]
    try:
        for child in children:
            child.start()
        claimed = [await asyncio.to_thread(queue.get, timeout=30) for _ in children]
        assert sum(claimed) == 1
        for child in children:
            await asyncio.to_thread(child.join, timeout=10)
            assert child.exitcode == 0
        assert setup.store.execution.get(accepted.run_id)["operation_calls"] == 1
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
        queue.close()


@pytest.mark.asyncio
async def test_stage8_downgrade_refuses_existing_coordination_facts(setup, monkeypatch):
    """普通回滚不能抹去协调事实，数据仍保持可恢复。"""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(root / "financeclaw/shared/infrastructure/migrations")
    )
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", setup.settings.database_url.get_secret_value())
    command.stamp(config, "0009_stage8a")
    _, accepted = await admit(setup)
    with pytest.raises(RuntimeError, match="archival"):
        command.downgrade(config, "0008_stage6fix_c")
    with setup.store.sessions() as session:
        assert session.get(CoordinatedRunRow, accepted.run_id).active


@pytest.mark.asyncio
async def test_child_query_preserves_owner_and_does_not_return_parent_journal(setup):
    """子运行 GET 保留其身份、等待和输出，父完成后也不冒充父 Journal。"""
    setup.backend.delegate = True
    _, accepted = await admit(setup)
    for _ in range(4):
        await tick(setup)
    root = await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    request = root.pending_interactions[0]
    child_id = request["owner_run_id"]
    child = await setup.bff.status(child_id, tenant_id="tenant", subject_id="subject")
    assert child.run_id == child_id and child.thread_id != root.thread_id
    assert child.status == "interrupted" and child.output is None
    await setup.services.conversations.interactions.respond(
        request["interaction_id"],
        InteractionResponse(revision=request["revision"], kind="input", answer={}),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="child-answer",
    )
    for _ in range(4):
        await tick(setup)
    child = await setup.bff.status(child_id, tenant_id="tenant", subject_id="subject")
    root = await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    assert child.run_id == child_id and child.status == "completed"
    assert child.output == {"message": "final answer"}
    assert child.output != root.output
