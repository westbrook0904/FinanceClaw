"""8C 的旧根原始证据、纯查询、暂停与唯一驱动接管回归。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from financeclaw.coordination.application.conversation_runs import ConversationRunService
from financeclaw.coordination.application.run_observation import observe_run
from financeclaw.coordination.application.run_service import RunNotFound
from financeclaw.coordination.backends.langgraph_backend import LangGraphBackend
from financeclaw.coordination.backends.langgraph_migration import LangGraphLegacyInspector
from financeclaw.coordination.backends.ports.agent_server import ServerRun
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.coordination.deployment import DeploymentControl, diagnostics
from financeclaw.coordination.migration import LegacyMigration
from financeclaw.coordination.repository import DRIVER_VERSION, StaleCoordinator, now
from financeclaw.kernel.delegation.models import AgentHandoff
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.conversation.tables import ConversationMessageRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.coordination_tables import (
    CoordinatedRunRow,
    RunAuthorizationRow,
)
from financeclaw.shared.execution_ledger.cutover_tables import LegacyAdoptionRow
from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from tests.stage8.test_coordinator import admit, tick


class LegacyNative:
    """可观察的旧 Agent Server；测试实际 legacy 受理与正式 LangGraph 映射器。"""

    def __init__(self):
        """保存固定原生尝试，允许测试未知回执与不静止 backend。"""
        self.runs, self.states = {}, {}
        self.calls = self.reads = 0
        self._client = SimpleNamespace(runs=SimpleNamespace(get=self.get))

    async def create_thread(self, _thread_id):
        """合成线程由首次 start 固定。"""

    async def create_run(self, **kwargs):
        """旧服务真正通过执行账本提交一次原始命令。"""
        self.calls += 1
        native_id = f"native-{self.calls}"
        self.runs[native_id] = {
            **kwargs,
            "run_id": native_id,
            "status": "success",
            "created_at": now().isoformat(),
        }
        self.states[native_id] = {
            "checkpoint": {"checkpoint_id": "checkpoint:" + native_id},
            "metadata": {"run_id": native_id, "step": 2},
            "values": {"messages": [{"type": "ai", "content": "旧尝试的原始结果"}]},
            "created_at": now().isoformat(),
            "next": [],
        }
        return ServerRun(native_id, "pending")

    async def find_operation(self, *, thread_id, operation_id):
        """只按原 operation 查找，不按 latest 或业务身份猜测。"""
        self.reads += 1
        for native_id, run in self.runs.items():
            if run["thread_id"] == thread_id and run["metadata"]["operation_id"] == operation_id:
                return ServerRun(native_id, run["status"])
        return None

    async def get(self, thread_id, run_id):
        """Adapter 的确切 runs.get，不走有推进副作用的 legacy status。"""
        self.reads += 1
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]

    async def _run_state(self, thread_id, run_id):
        """原尝试的固定 checkpoint。"""
        self.reads += 1
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.states[run_id]


async def legacy(setup):
    """用升级前服务产生完整原始命令和 Journal，不逆改新协调行伪造旧数据。"""
    native = LegacyNative()
    service = ConversationRunService(
        native, setup.store.journal, setup.services.releases.agent_profiles
    )
    conversation = await setup.bff.create(tenant_id="tenant", subject_id="subject")
    accepted = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="原始冻结输入"),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="legacy-key",
    )
    backend = LangGraphBackend(
        setup.settings, setup.store, setup.services.background_releases, native=native
    )
    migration = LegacyMigration(
        setup.store, setup.services.background_releases, LangGraphLegacyInspector(backend)
    )
    return SimpleNamespace(
        conversation=conversation,
        accepted=accepted,
        native=native,
        backend=backend,
        migration=migration,
        service=service,
    )


def seal(setup):
    """合成进程均已停止；正式部署必须提供真实停机与阻止重启的证据。"""
    control = DeploymentControl(setup.store)
    first = control.change(0, admission_paused=True, dispatch_paused=True)
    return control.change(
        first["revision"],
        admission_paused=True,
        dispatch_paused=True,
        stopped_evidence_hash=digest("synthetic stopped producers"),
    )


async def take_over(setup, old):
    """影子检查的两个摘要与控制 revision 必须保持一致。"""
    gate = seal(setup)
    plan = await old.migration.shadow(old.accepted.run_id)
    assert plan["state"] == "ready_for_reauthorization", old.migration.public(plan)
    result = await old.migration.adopt(
        old.accepted.run_id,
        fingerprint=plan["fingerprint"],
        shadow_hash=plan["shadow_hash"],
        control_revision=gate["revision"],
    )
    return plan, result


@pytest.mark.asyncio
async def test_shadow_and_takeover_preserve_facts_and_never_resubmit(setup):
    """原请求、时钟、预算与回执归档；接管要新授权，但不新增 start。"""
    old = await legacy(setup)
    root_id = old.accepted.run_id
    saved = setup.store.execution.get(root_id)
    plan = await old.migration.shadow(root_id)
    assert plan["state"] == "ready_for_reauthorization"
    assert setup.store.execution.get(root_id) == saved
    assert old.native.calls == 1
    with setup.store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CoordinatedRunRow)) == 0
    plan, result = await take_over(setup, old)
    assert result["driver_version"] == DRIVER_VERSION
    replay = await setup.bff.start_turn(
        old.conversation.conversation_id,
        ConversationTurnRequest(message="原始冻结输入"),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="legacy-key",
    )
    assert replay.run_id == root_id
    current = setup.store.execution.get(root_id)
    assert current["operation_calls"] == saved["operation_calls"]
    assert current["snapshot"]["context"] == saved["snapshot"]["context"]
    with setup.store.sessions() as session:
        archive = session.get(LegacyAdoptionRow, root_id)
        assert digest(archive.original) == plan["fingerprint"]
        assert session.get(RunAuthorizationRow, root_id).revoked
    setup.coordinator.backend = old.backend
    await tick(setup)
    assert old.native.calls == 1
    assert (
        await setup.bff.status(root_id, tenant_id="tenant", subject_id="subject")
    ).waiting_reason == "authorization_required"
    await setup.services.conversations.reauthorize(
        root_id, tenant_id="tenant", subject_id="subject", scopes=setup.scopes
    )
    DeploymentControl(setup.store).change(2, admission_paused=False, dispatch_paused=False)
    await tick(setup)
    assert (
        await setup.bff.status(root_id, tenant_id="tenant", subject_id="subject")
    ).status == "completed"
    assert old.native.calls == 1
    with setup.store.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ConversationMessageRow)
                .where(ConversationMessageRow.role == "assistant")
            )
            == 1
        )
    with pytest.raises(ExecutionConflict):
        await old.service.operations.submit_prepared(
            next(iter(plan["commands"].values())).operation_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["snapshot", "resume", "active", "unknown", "wrong_receipt"])
async def test_incomplete_legacy_evidence_stays_visible_without_writes(setup, fault):
    """无法证明的旧根清楚留在盘点结果中，回调或查无结果不能重启它。"""
    old = await legacy(setup)
    root_id = old.accepted.run_id
    with setup.store.sessions.begin() as session:
        execution = session.get(RunExecutionRow, root_id)
        operation = session.scalar(select(RunOperationRow).where(RunOperationRow.run_id == root_id))
        if fault == "snapshot":
            execution.snapshot = {**execution.snapshot, "profile": {}}
        elif fault == "resume":
            operation.request = {**operation.request, "command": {"resume": "old decision"}}
            operation.request_hash = digest(operation.request)
        elif fault == "unknown":
            operation.server_run_id = execution.server_run_id = None
            operation.status = "uncertain"
            old.native.runs.clear()
        elif fault == "wrong_receipt":
            operation.server_run_id = "different-native"
        else:
            old.native.runs[operation.server_run_id]["status"] = "running"
    plan = await old.migration.shadow(root_id)
    assert plan["state"] == "blocked" and plan["reasons"]
    assert old.native.calls == 1
    with setup.store.sessions() as session:
        assert session.get(CoordinatedRunRow, root_id) is None


@pytest.mark.asyncio
async def test_takeover_requires_stopped_proof_and_current_fingerprint(setup):
    """先只读检查不能代替停止生产者；快照修改和重复接管均被 CAS 拒绝。"""
    old = await legacy(setup)
    root_id = old.accepted.run_id
    plan = await old.migration.shadow(root_id)
    with pytest.raises(ExecutionConflict, match="stopped legacy"):
        await old.migration.adopt(
            root_id,
            fingerprint=plan["fingerprint"],
            shadow_hash=plan["shadow_hash"],
            control_revision=0,
        )
    seal(setup)
    with setup.store.sessions.begin() as session:
        session.get(RunExecutionRow, root_id).tool_calls += 1
    with pytest.raises(ExecutionConflict, match="changed"):
        await old.migration.adopt(
            root_id,
            fingerprint=plan["fingerprint"],
            shadow_hash=plan["shadow_hash"],
            control_revision=2,
        )
    with setup.store.sessions() as session:
        assert session.get(LegacyAdoptionRow, root_id) is None


@pytest.mark.asyncio
async def test_disabled_admission_keeps_all_reads_pure_and_existing_responsibility(setup):
    """关闭受理后继续查询与推进已受理根，旧根查询不会调用 backend。"""
    _, accepted = await admit(setup)
    setup.services.conversations.settings = setup.settings.model_copy(
        update={"coordinator_enabled": False}
    )
    await tick(setup)
    for _ in range(3):
        await setup.bff.status(accepted.run_id, tenant_id="tenant", subject_id="subject")
    assert setup.backend.calls == 1 and setup.backend.reads == 0
    await tick(setup)
    conversation = await setup.bff.create(tenant_id="tenant", subject_id="subject")
    with pytest.raises(ExecutionConflict, match="paused"):
        await setup.bff.start_turn(
            conversation.conversation_id,
            ConversationTurnRequest(message="new"),
            tenant_id="tenant",
            subject_id="subject",
            scopes=setup.scopes,
            idempotency_key="new",
        )
    with setup.store.sessions() as session:
        assert not session.scalar(
            select(ConversationTurnRow).where(
                ConversationTurnRow.conversation_id == conversation.conversation_id
            )
        )


@pytest.mark.asyncio
async def test_deployment_pause_old_versions_and_late_leases(setup):
    """封闭把原版本责任升到 3，过期租约不能继续写；暂停不扣预算或派发。"""
    _, accepted = await admit(setup)
    with setup.store.sessions.begin() as session:
        session.get(CoordinatedRunRow, accepted.run_id).driver_version = 1
    claim = setup.store.claim_due("old-process", lease_seconds=30)
    gate = seal(setup)
    with pytest.raises(StaleCoordinator):
        setup.store.finish(claim, delay=0)
    await tick(setup)
    assert setup.backend.calls == 0
    assert setup.store.execution.get(accepted.run_id)["operation_calls"] == 0
    control = DeploymentControl(setup.store)
    with pytest.raises(ExecutionConflict, match="revision"):
        control.change(0, admission_paused=False, dispatch_paused=False)
    control.change(gate["revision"], admission_paused=False, dispatch_paused=False)
    assert control.view()["legacy_fenced"]
    await tick(setup)
    assert setup.backend.calls == 1


@pytest.mark.asyncio
async def test_diagnostics_readiness_detects_backlog_and_missing_worker(setup):
    """过度积压和无兼容处理者使 BFF 不就绪；诊断不暴露任务载荷。"""
    _, accepted = await admit(setup)
    assert not await setup.services.conversations.healthy()
    setup.store.heartbeat("worker")
    assert await setup.services.conversations.healthy()
    with setup.store.sessions.begin() as session:
        session.get(CoordinatedRunRow, accepted.run_id).due_at = now() - timedelta(seconds=300)
    assert not await setup.services.conversations.healthy()
    state = diagnostics(setup.store)
    assert state["oldest_due_seconds"] >= 300 and state["due_roots"] == 1
    assert accepted.run_id not in str(state) and "synthetic request" not in str(state)


async def legacy_child(setup, old, *, question):
    """实际旧委派服务生成独立 child 和冻结上下文；父已停在确切 handoff。"""
    services = build_coordination(
        setup.settings, resources=setup.services.resources, client=old.native
    )
    root_id = old.accepted.run_id
    execution = setup.store.execution.get(root_id)
    context = execution["snapshot"]["context"]
    handoff = AgentHandoff(
        handoff_id="legacy-delegation",
        parent_run_id=root_id,
        parent_turn_id=context["turn_id"],
        conversation_id=context["conversation_id"],
        agent_id="market_research_agent",
        task="synthetic research",
    )
    native_id = execution["server_run_id"]
    old.native.runs[native_id]["status"] = "interrupted"
    old.native.states[native_id]["interrupts"] = [
        {"id": "parent-wait", "value": handoff.model_dump(mode="json")}
    ]
    record = await services.delegations.start(
        handoff,
        parent_run_id=root_id,
        parent_turn_id=context["turn_id"],
        conversation_id=context["conversation_id"],
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        parent_snapshot=execution["snapshot"],
        parent_server_run_id=native_id,
        parent_interrupt_id="parent-wait",
    )
    child = setup.store.execution.get(record.child_run_id)
    child_native = child["server_run_id"]
    pending = None
    if question:
        payload = {
            "schema_version": 1,
            "kind": "user_interaction",
            "interaction_kind": "input",
            "point_id": "research_scope",
            "question": "请补充本次研究的时间区间。",
        }
        old.native.runs[child_native]["status"] = "interrupted"
        old.native.states[child_native]["interrupts"] = [{"id": "child-wait", "value": payload}]
        pending = await services.delegations.interactions.observe_agent(
            record.child_run_id,
            observe_run({"interrupts": old.native.states[child_native]["interrupts"]}),
            server_run_id=child_native,
            expires_at=now() + timedelta(seconds=600),
            checkpoint_id="checkpoint:" + child_native,
        )
    else:
        old.native.states[child_native]["values"] = {
            "structured_response": {
                "outcome": "partial",
                "summary": "synthetic result",
                "limitations": ["test only"],
            }
        }
    return record, pending


@pytest.mark.asyncio
async def test_child_interaction_keeps_original_id_deadline_and_frozen_context(setup):
    """接管父子两处 checkpoint；旧问题、权限、时钟与资料快照继续使用。"""
    old = await legacy(setup)
    record, pending = await legacy_child(setup, old, question=True)
    original = setup.store.execution.get(record.child_run_id)["snapshot"]
    await take_over(setup, old)
    adopted = setup.store.execution.get(record.child_run_id)["snapshot"]
    assert adopted["context"] == original["context"]
    assert adopted["resolved_context"] == original["resolved_context"]
    with setup.store.sessions() as session:
        saved = session.get(PendingInteractionRow, pending["interaction_id"])
        assert saved.request["coordination"]["request_id"] == pending["interaction_id"]
        assert saved.revision == pending["revision"]
        from financeclaw.coordination.repository import aware

        assert aware(saved.expires_at) == pending["expires_at"]
    await setup.services.conversations.reauthorize(
        old.accepted.run_id, tenant_id="tenant", subject_id="subject", scopes=setup.scopes
    )
    setup.coordinator.backend = old.backend
    await tick(setup)
    await setup.services.conversations.interactions.respond(
        pending["interaction_id"],
        InteractionResponse(
            revision=pending["revision"], kind="input", answer={"analysis_period": "one year"}
        ),
        tenant_id="tenant",
        subject_id="subject",
        scopes=setup.scopes,
        idempotency_key="old-question-answer",
    )
    with setup.store.sessions() as session:
        decision = session.get(PendingInteractionRow, pending["interaction_id"])
        command = session.get(RunOperationRow, decision.operation_id)
        assert command.request["payload"]["request"]["owner_task_id"] == record.child_run_id
        assert session.get(DelegationRow, record.delegation_id).child_run_id == record.child_run_id
    assert old.native.calls == 2


@pytest.mark.asyncio
async def test_adopted_child_result_prepares_original_parent_delivery_without_new_child(setup):
    """尚未交付的旧 child 成功结果进入原父 continuation，复用原 child 和操作 ID。"""
    old = await legacy(setup)
    record, _ = await legacy_child(setup, old, question=False)
    await take_over(setup, old)
    await setup.services.conversations.reauthorize(
        old.accepted.run_id, tenant_id="tenant", subject_id="subject", scopes=setup.scopes
    )
    setup.coordinator.backend = old.backend
    await tick(setup)
    delivery = setup.store.execution.operation(
        "operation-" + digest([old.accepted.run_id, "delivery:" + record.delegation_id])
    )
    assert delivery["status"] == "prepared"
    assert delivery["request"]["payload"]["responding_task_id"] == record.child_run_id
    assert old.native.calls == 2


@pytest.mark.asyncio
async def test_missing_snapshot_and_historical_terminal_remain_readable_and_paginated(setup):
    """最早期缺执行快照的 Turn 仍可只读查询；历史终态不会复活或创建通知。"""
    conversation = await setup.bff.create(tenant_id="tenant", subject_id="subject")
    turn, _, _ = setup.store.journal.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant",
        subject_id="subject",
        idempotency_key="orphan",
        request_hash="a" * 64,
        message="old journal only",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.4.0",
    )
    status = await setup.bff.status(turn.run_id, tenant_id="tenant", subject_id="subject")
    assert status.waiting_reason == "legacy_migration_required"
    with pytest.raises(RunNotFound):
        await setup.bff.status(turn.run_id, tenant_id="another", subject_id="subject")
    old = await legacy(setup)
    first = old.migration.inventory(limit=1)
    second = old.migration.inventory(limit=1, after=first["next_cursor"])
    assert {first["items"][0]["run_id"], second["items"][0]["run_id"]} == {
        turn.run_id,
        old.accepted.run_id,
    }
    assert second["next_cursor"] is None
    setup.store.journal.append_assistant_message(run_id=turn.run_id, content="historical answer")
    status = await setup.bff.status(turn.run_id, tenant_id="tenant", subject_id="subject")
    assert status.status == "completed"
    assert "historical answer" in str(status.output)
    assert (await old.migration.shadow(turn.run_id))["state"] == "historical_terminal"
    assert old.native.calls == 1


@pytest.mark.asyncio
async def test_worker_survives_database_failure_before_claim_and_after_step(setup, monkeypatch):
    """心跳与 finish 暂时失败不会退出循环；原租约过期后继续且不重复提交。"""
    from financeclaw.coordination.worker.__main__ import run_worker

    _, accepted = await admit(setup)
    setup.coordinator.settings = setup.settings.model_copy(
        update={
            "coordinator_lease_seconds": 3,
            "coordinator_poll_seconds": 0.05,
            "coordinator_reconcile_seconds": 0.1,
            "coordinator_worker_concurrency": 1,
        }
    )
    original_heartbeat, original_finish = setup.store.heartbeat, setup.store.finish
    failures = {"heartbeat": 0, "finish": 0}

    def heartbeat(*args, **kwargs):
        """领取前的数据库连接暂时不可用。"""
        failures["heartbeat"] += 1
        if failures["heartbeat"] == 1:
            raise TimeoutError("synthetic database outage")
        return original_heartbeat(*args, **kwargs)

    def finish(*args, **kwargs):
        """已持久绑定回执后，释放责任的事务连接暂时不可用。"""
        failures["finish"] += 1
        if failures["finish"] == 1:
            raise TimeoutError("synthetic database outage")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(setup.store, "heartbeat", heartbeat)
    monkeypatch.setattr(setup.store, "finish", finish)
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(setup.coordinator, stop))
    try:
        for _ in range(200):
            assert not worker.done()
            with setup.store.sessions() as session:
                if not session.get(CoordinatedRunRow, accepted.run_id).active:
                    break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("worker did not recover its original lease")
        assert setup.backend.calls == 1
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
    assert failures["finish"] >= 2


@pytest.mark.asyncio
async def test_slow_backend_keeps_worker_ready_with_default_lease(setup, monkeypatch):
    """默认 60 秒租约的远程调用超过心跳宽限时，独立续租仍保持 readiness。"""
    from financeclaw.coordination.worker.__main__ import run_worker

    await admit(setup)
    setup.coordinator.settings = setup.settings.model_copy(
        update={"coordinator_worker_concurrency": 1}
    )
    started, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_submit = setup.backend.submit_task

    async def slow_submit(command):
        """模拟已领取而仍在等待原提交响应的慢 backend。"""
        started.set()
        await release.wait()
        return await original_submit(command)

    monkeypatch.setattr(setup.backend, "submit_task", slow_submit)
    worker = asyncio.create_task(run_worker(setup.coordinator, stop))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(10.5)
        assert await setup.services.conversations.healthy()
    finally:
        stop.set()
        release.set()
        await asyncio.wait_for(worker, timeout=5)


def test_disabled_new_admission_still_requires_valid_worker_callback(setup):
    """关闭新受理的 Worker 继续处理旧责任，不能因此跳过 callback 配置校验。"""
    from financeclaw.coordination.bootstrap import build_coordinator
    from financeclaw.shared.infrastructure.settings import FinanceClawSettings

    services, _ = build_coordinator(
        setup.settings.model_copy(update={"coordinator_enabled": False})
    )
    try:
        assert not services.conversations.settings.coordinator_enabled
        assert services.conversations.coordinated
    finally:
        services.resources.database.close()
    with pytest.raises(ValueError, match="callback"):
        FinanceClawSettings(
            _env_file=None,
            environment="test",
            coordinator_enabled=False,
            coordinator_callback_url="https://invalid.example/wrong-path",
            coordinator_webhook_token="synthetic-callback-configuration-0000",
        )
