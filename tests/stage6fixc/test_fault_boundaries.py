"""交互决定与远程恢复之间的故障窗口、旧接口和撤权回归。"""

import asyncio
from datetime import datetime, timedelta

import pytest

from financeclaw.application.interaction_service import InteractionService
from financeclaw.kernel import ApprovalDecision
from financeclaw.modules.execution import ExecutionConflict
from tests.stage6fix.test_execution_recovery import OWNER
from tests.stage6fixc.test_interactions import SCOPES, parent_approval, reply


@pytest.mark.parametrize("accepted_remotely", [True, False])
@pytest.mark.asyncio
async def test_interaction_receipt_loss_never_resubmits(tmp_path, monkeypatch, accepted_remotely):
    """C03：未知回执只查原 operation；查不到也不能把超时视为未执行。"""
    _, fake, _, service, accepted, item = await parent_approval(tmp_path)
    submit = fake.submit_resume

    async def lose_receipt(**kwargs):
        """分别模拟远程受理之后和网络发送之前失去响应。"""
        if accepted_remotely:
            await submit(**kwargs)
        raise TimeoutError("injected missing response")

    monkeypatch.setattr(fake, "submit_resume", lose_receipt)
    first = await service.interactions.respond(
        item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="lost", **OWNER
    )
    assert first["resume_status"] == "uncertain"
    service.interactions = InteractionService(
        fake, service.execution, agent_profiles=service.agent_profiles
    )
    for _ in range(3):
        await service.interactions.respond(
            item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="lost", **OWNER
        )
        state = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    assert len(fake.resume_calls) == (2 if accepted_remotely else 1)
    assert state.status == ("completed" if accepted_remotely else "interrupted")
    if not accepted_remotely:
        assert state.waiting_reason == "submission_uncertain"
        assert (await service.cancel(accepted.run_id, **OWNER)).status == "cancellation_requested"


@pytest.mark.parametrize("after_acceptance", ["expiry", "cancel", "revoked"])
@pytest.mark.asyncio
async def test_prepared_answer_cannot_bypass_expiry_cancel_or_revocation(
    tmp_path, monkeypatch, after_acceptance
):
    """已接受不等于已提交；宕机期间发生的过期、取消和撤权必须再次检查。"""
    _, fake, _, service, accepted, item = await parent_approval(tmp_path)
    submit = service.interactions.operations.submit_prepared

    async def crash(_operation):
        """事务已提交，但还没领取出站提交权。"""
        raise RuntimeError("injected before claim")

    monkeypatch.setattr(service.interactions.operations, "submit_prepared", crash)
    with pytest.raises(RuntimeError):
        await service.interactions.respond(
            item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="prepared", **OWNER
        )
    monkeypatch.setattr(service.interactions.operations, "submit_prepared", submit)
    if after_acceptance == "expiry":
        service._clock = lambda: datetime.fromisoformat(item["expires_at"]) + timedelta(seconds=1)
        result = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
        assert result.waiting_reason == "expired_before_submission"
    elif after_acceptance == "cancel":
        assert (await service.cancel(accepted.run_id, **OWNER)).status == "cancelled"
    else:
        with pytest.raises(ExecutionConflict, match="authorization"):
            await service.status(accepted.run_id, scopes=frozenset(), **OWNER)
        with pytest.raises(ExecutionConflict, match="authorization"):
            await service.interactions.respond(
                item["interaction_id"],
                reply(item),
                scopes=frozenset(),
                idempotency_key="prepared",
                **OWNER,
            )
    if after_acceptance != "revoked":
        repeated = await service.interactions.respond(
            item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="prepared", **OWNER
        )
        assert repeated["status"] == "resolved"
        assert repeated["resume_status"].endswith("_before_submission")
    assert len(fake.resume_calls) == 1


@pytest.mark.asyncio
async def test_cancel_between_claim_and_receipt_does_not_release_active_turn(tmp_path, monkeypatch):
    """回答已领取但未取得回执时，取消只能等待停止确认，不能释放共享线程。"""
    _, fake, _, service, accepted, item = await parent_approval(tmp_path)
    submit = fake.submit_resume
    claimed, proceed = asyncio.Event(), asyncio.Event()

    async def blocked_submit(**kwargs):
        """停在已领取和真正远程提交之间，覆盖取消的最窄竞争窗口。"""
        claimed.set()
        await proceed.wait()
        return await submit(**kwargs)

    monkeypatch.setattr(fake, "submit_resume", blocked_submit)
    responding = asyncio.create_task(
        service.interactions.respond(
            item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="race", **OWNER
        )
    )
    try:
        await asyncio.wait_for(claimed.wait(), timeout=5)
        assert (await service.cancel(accepted.run_id, **OWNER)).status == "cancellation_requested"
    finally:
        proceed.set()
        await responding
    assert (await service.cancel(accepted.run_id, **OWNER)).status == "cancelled"
    await service.interactions.respond(
        item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="race", **OWNER
    )
    assert len(fake.resume_calls) == 2


@pytest.mark.asyncio
async def test_legacy_approval_replays_original_instance_after_receipt_changes(tmp_path):
    """旧入口的幂等关联依赖 interrupt，不因最新 server run 已改变而另起操作。"""
    _, fake, _, service, accepted, item = await parent_approval(tmp_path)
    decision = ApprovalDecision(
        type="approve", interrupt_id=item["interrupt_id"], arguments_hash=item["arguments_hash"]
    )
    for _ in range(3):
        assert await service.interactions.resume_legacy(
            accepted.run_id, decision, scopes=SCOPES, **OWNER
        )
    assert len(fake.resume_calls) == 2
    with pytest.raises(ExecutionConflict):
        await service.interactions.resume_legacy(
            accepted.run_id,
            decision.model_copy(update={"interrupt_id": "not-the-instance"}),
            scopes=SCOPES,
            **OWNER,
        )
