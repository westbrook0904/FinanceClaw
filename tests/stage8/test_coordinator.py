"""正式受理／Worker 的无前台闭环与故障边界；backend 桩没有 LangGraph 原生类型。"""

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from financeclaw.bff.application.conversation_service import ConversationService
from financeclaw.coordination.application.coordinator import Coordinator
from financeclaw.coordination.application.releases import release_ref
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.coordination.repository import now
from financeclaw.kernel.coordination import (
    BackendCapabilities,
    BackendExecutionRef,
    BackendObservation,
    CancellationReceipt,
    ContinuationRef,
    DelegationRequest,
    InteractionRequest,
    ResponseApplicationEvidence,
    ResponseDelivery,
    SubmissionReceipt,
    bounded_digest,
    handoff_input,
)
from financeclaw.kernel.delegation.models import AgentHandoff
from financeclaw.kernel.interactions import InteractionPoint, InteractionResponse
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.coordination_tables import (
    CoordinatedRunRow,
    RunAuthorizationRow,
)
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


class Backend:
    """行为测试端口；opaque ID、等待地址和事件都不是 LangGraph 对象。"""

    capabilities = BackendCapabilities(
        exact_observation=True,
        durable_continuation=True,
        recoverable_requests=True,
        operation_lookup=True,
        cancellation_confirmation=True,
        response_application_evidence=True,
    )

    def __init__(self, store, releases, *, delegate=False, lose_receipt=False):
        """初始化独立 backend 的远端操作与固定请求记录。"""
        self.store, self.releases = store, releases
        self.delegate, self.lose_receipt = delegate, lose_receipt
        self.commands, self.references, self.requests = {}, {}, {}
        self.calls = self.reads = self.cancels = 0

    def accept(self, command):
        """一次真实副作用后可故意丢回执。"""
        self.calls += 1
        task_id = (
            command.request.owner_task_id
            if isinstance(command, ResponseDelivery)
            else command.task_id
        )
        reference = BackendExecutionRef(
            backend_instance_id=self.store.backend_instance_id,
            task_id=task_id,
            operation_id=command.operation_id,
            execution_id="remote:" + command.operation_id,
        )
        self.commands[command.operation_id], self.references[command.operation_id] = (
            command,
            reference,
        )
        if self.lose_receipt:
            raise ConnectionError("synthetic receipt loss")
        return SubmissionReceipt(status="submitted", execution_ref=reference)

    async def submit_task(self, command):
        """接收 root 或 child。"""
        return self.accept(command)

    async def deliver_response(self, command):
        """接收对确切请求的响应。"""
        return self.accept(command)

    async def lookup_operation(self, command):
        """按原操作查回执，不允许重发。"""
        ref = self.references.get(command.operation_id)
        return (
            SubmissionReceipt(status="submitted", execution_ref=ref)
            if ref
            else SubmissionReceipt(status="uncertain")
        )

    async def request_cancel(self, reference, *, operation_id):
        """确认确切远端尝试停止。"""
        self.cancels += 1
        return CancellationReceipt(execution_ref=reference, status="confirmed")

    async def observe_execution(self, reference):
        """合成 root→child→用户交互→child→root，不依赖任何前台观察。"""
        self.reads += 1
        command = self.commands[reference.operation_id]
        if isinstance(command, ResponseDelivery):
            proof = ResponseApplicationEvidence(
                operation_id=command.operation_id,
                request_id=command.request.request_id,
                continuation_id=command.request.continuation_ref.continuation_id,
                execution_ref=reference,
                response_hash=bounded_digest(command.response.model_dump(mode="json")),
                checkpoint_ref="proof:" + command.operation_id,
            )
            return BackendObservation(
                execution_ref=reference,
                status="completed",
                result={"message": "final answer"},
                evidence_ref="done",
                response_applications=(proof,),
            )
        if not self.delegate:
            return BackendObservation(
                execution_ref=reference,
                status="completed",
                result={"message": "final answer"},
                evidence_ref="done",
            )
        if reference.operation_id not in self.requests:
            snapshot = self.store.execution.get(reference.task_id)["snapshot"]
            context = snapshot["context"]
            native = {"position": reference.execution_id}
            if reference.task_id == context["root_run_id"]:
                handoff = AgentHandoff(
                    handoff_id="request:" + reference.task_id,
                    parent_run_id=reference.task_id,
                    parent_turn_id=context["turn_id"],
                    conversation_id=context["conversation_id"],
                    agent_id="market_research_agent",
                    task="synthetic research",
                )
                from financeclaw.shared.execution_ledger.repository import snapshot_context
                from financeclaw.shared.execution_ledger.snapshots import agent_snapshot

                target = agent_snapshot(
                    self.releases.agents.resolve("market_research_agent", "1.2.0"),
                    snapshot_context(snapshot),
                    thread_id="unused",
                    input_hash="",
                )
                values = {"handoff": handoff, "target": release_ref(target)}
                identity, input_hash, contract = (
                    handoff.handoff_id,
                    bounded_digest(handoff_input(handoff)),
                    DelegationRequest,
                )
            else:
                point = InteractionPoint(
                    point_id="scope",
                    kind="input",
                    question="Confirm scope",
                    response_schema={"type": "object"},
                )
                identity, contract = "question:" + reference.task_id, InteractionRequest
                values = {
                    "point": point.model_dump(mode="json"),
                    "revision": 1,
                    "question": point.question,
                    "expires_at": (now() + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                    "action_hash": None,
                }
                input_hash = bounded_digest(values)
            continuation = ContinuationRef(
                continuation_id="continue:" + reference.operation_id,
                request_id=identity,
                source_execution_ref=reference,
                release=release_ref(snapshot),
                input_hash=input_hash,
                binding_hash=bounded_digest(native),
            )
            request = contract(
                request_id=identity,
                root_task_id=context["root_run_id"],
                owner_task_id=reference.task_id,
                source_execution_ref=reference,
                continuation_ref=continuation,
                input_hash=input_hash,
                **values,
            )
            self.requests[reference.operation_id] = request
        request = self.requests[reference.operation_id]
        return BackendObservation(
            execution_ref=reference,
            status="waiting",
            requests=(request,),
            continuation_bindings={
                request.continuation_ref.continuation_id: {"position": reference.execution_id}
            },
            evidence_ref="paused",
        )


@pytest.fixture
def setup(tmp_path):
    """真实 SQL 仓储和正式发布配置，只有 backend 被行为桩替代。"""
    postgres = os.environ.get("FINANCECLAW_STAGE8_TEST_POSTGRES_URL")
    database_url = f"sqlite:///{tmp_path / 'app.db'}"
    if postgres:
        from experiments.stage8.run import isolated_database

        database_url = isolated_database(postgres).replace("postgresql://", "postgresql+psycopg://")
    settings = FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        database_url=database_url,
        database_auto_create_schema=True,
        coordinator_enabled=True,
        coordinator_callback_url="http://localhost:8081/internal/webhooks/langgraph-primary",
        coordinator_webhook_token="synthetic-verification-credential-0000",
        artifact_root=str(tmp_path / "artifacts"),
    )
    services = build_coordination(settings)
    bff = ConversationService(
        services.resources.conversation_repository,
        services.releases.agent_profiles,
        runs=services.conversations,
    )
    backend = Backend(services.background_repository, services.background_releases)
    coordinator = Coordinator(
        services.background_repository, services.background_releases, backend, settings
    )
    bundle = SimpleNamespace(
        settings=settings,
        services=services,
        bff=bff,
        backend=backend,
        coordinator=coordinator,
        store=services.background_repository,
        scopes=frozenset({"market:read", "tools:read", "artifacts:read"}),
    )
    yield bundle
    services.resources.database.close()
    if postgres:
        from urllib.parse import urlsplit

        import psycopg
        from psycopg import sql

        with psycopg.connect(postgres, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(urlsplit(database_url).path.lstrip("/"))
                )
            )


async def admit(setup):
    """通过实际 BFF 应用受理接口开启任务。"""
    conversation = await setup.bff.create(tenant_id="tenant", subject_id="subject")
    result = await setup.bff.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="synthetic request"),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="input-key",
    )
    return conversation, result


async def tick(setup):
    """领取并推进一小步；测试不通过 status 触发任何执行。"""
    claim = setup.store.claim_due("worker", lease_seconds=10)
    if claim:
        try:
            await setup.coordinator.advance(claim)
        finally:
            setup.store.finish(claim, delay=0)


@pytest.mark.asyncio
async def test_admission_is_local_and_completes_without_frontend(setup):
    """受理和重复 GET 零 backend 调用，关闭前台后 Worker 原子完成 Journal。"""
    conversation, accepted = await admit(setup)
    for _ in range(3):
        assert (
            await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
        ).status == "accepted"
    assert setup.backend.calls == setup.backend.reads == 0
    await tick(setup)
    await tick(setup)
    assert setup.backend.calls == 1
    assert (
        await setup.bff.assistant_content(accepted.run_id, tenant_id="tenant", subject_id="subject")
        == "final answer"
    )
    assert len(setup.store.journal.list_messages(conversation.conversation_id)) == 2


@pytest.mark.asyncio
async def test_root_child_user_decision_and_original_parent_complete(setup):
    """真正等待回答的 child 恢复，再将结果交回原 parent，最终仅一条助手消息。"""
    setup.backend.delegate = True
    conversation, accepted = await admit(setup)
    for _ in range(4):
        await tick(setup)
    status = await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    assert status.waiting_reason == "input_required"
    request = status.pending_interactions[0]
    assert request["owner_run_id"] != accepted.run_id
    await setup.services.conversations.interactions.respond(
        request["interaction_id"],
        InteractionResponse(
            revision=request["revision"], kind="input", answer={"scope": "synthetic"}
        ),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="decision",
    )
    assert setup.backend.calls == 2
    for _ in range(4):
        await tick(setup)
    assert (
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    ).status == "completed"
    assert setup.backend.calls == 4
    assert len(setup.store.journal.list_messages(conversation.conversation_id)) == 2


@pytest.mark.asyncio
async def test_lost_receipt_looks_up_original_operation(setup):
    """远端成功本地丢回执后仍只有一次提交。"""
    setup.backend.lose_receipt = True
    _, accepted = await admit(setup)
    with pytest.raises(ConnectionError):
        await tick(setup)
    await tick(setup)
    await tick(setup)
    assert setup.backend.calls == 1
    assert (
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    ).status == "completed"


@pytest.mark.asyncio
async def test_failed_admission_rolls_back_all_facts(setup, monkeypatch):
    """模拟 Journal 已写、推进责任写失败，不能留下半个受理结果。"""

    def fail(*_):
        """在最后一个受理写入边界注入事务异常。"""
        raise RuntimeError("admission crash")

    monkeypatch.setattr(setup.store, "command_inbox", fail)
    with pytest.raises(RuntimeError):
        await admit(setup)
    with setup.store.sessions() as session:
        for table in (
            ConversationMessageRow,
            ConversationTurnRow,
            RunExecutionRow,
            RunOperationRow,
            CoordinatedRunRow,
            RunAuthorizationRow,
        ):
            assert session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.asyncio
async def test_cancel_before_worker_and_expired_grant_do_not_submit(setup):
    """取消和过期授权在固定派发边界复验。"""
    _, accepted = await admit(setup)
    with setup.store.sessions.begin() as session:
        session.get(RunAuthorizationRow, accepted.run_id).expires_at = now() - timedelta(seconds=1)
    await tick(setup)
    assert setup.backend.calls == 0
    assert (
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    ).waiting_reason == "authorization_required"
    await setup.bff.cancel(accepted.run_id, tenant_id="tenant", subject_id="subject")
    await tick(setup)
    assert (
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    ).status == "cancelled"
    assert setup.backend.calls == 0
