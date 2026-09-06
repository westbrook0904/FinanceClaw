"""A 阶段父子恢复、提交丢失、并发 Journal 和取消的持久化验收。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage

from financeclaw.application import ConversationService, DelegationService, WorkflowService
from financeclaw.kernel import ApprovalDecision, ConversationTurnRequest
from financeclaw.modules.conversation import ConversationConflict
from financeclaw.modules.execution import ExecutionConflict
from financeclaw.modules.execution.repository import digest
from tests.stage4.test_delegation import SCOPES, FakeDelegationClient, _components

OWNER = {"tenant_id": "tenant-a", "subject_id": "subject-a"}


def stack(tmp_path, fake=None):
    """服务共享真实数据库，只有 Agent Server 为可注入故障的假客户端。"""
    components = _components(tmp_path / "execution.db")
    client = fake or FakeDelegationClient()
    workflow = WorkflowService(
        client, components.workflow_repository, components.workflow_catalog, components.audit
    )
    delegation = DelegationService(
        client,
        components.delegation_repository,
        workflow,
        components.agent_profiles,
        components.audit,
        conversation_repository=components.conversation_repository,
        artifact_service=components.artifact_service,
    )
    conversations = ConversationService(
        client,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=delegation,
    )
    return components, client, delegation, conversations


async def started_child(conversations, fake, *, scopes=SCOPES):
    """启动根 Turn 和一个尚未完成的子任务。"""
    conversation = await conversations.create(**OWNER)
    accepted = await conversations.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="research AAPL"),
        scopes=scopes,
        idempotency_key="root",
        **OWNER,
    )
    waiting = await conversations.status(accepted.run_id, scopes=scopes, **OWNER)
    child = next(
        run for run in fake.runs.values() if run["metadata"].get("parent_run_id") == accepted.run_id
    )
    return accepted, waiting, child


class ParentApprovalClient(FakeDelegationClient):
    """子结果交付之后，父模型再提出一个独立写动作。"""

    async def resume_run(self, **kwargs):
        """真实形状的 HITL payload，避免以缺字段假中断代替框架验证。"""
        self.resume_calls.append(kwargs)
        payload = kwargs["command"]["resume"]
        if "delegation_id" in payload:
            return {
                "__interrupt__": [
                    {
                        "id": "parent-approval",
                        "value": {
                            "action_requests": [
                                {
                                    "name": "watchlist_add",
                                    "args": {"symbol": "AAPL", "note": "test"},
                                }
                            ],
                            "review_configs": [
                                {
                                    "action_name": "watchlist_add",
                                    "allowed_decisions": ["approve", "reject"],
                                }
                            ],
                        },
                    }
                ]
            }
        return {"messages": [AIMessage(content="approved parent result")]}


@pytest.mark.asyncio
async def test_child_delivery_then_parent_hitl_is_not_completed(tmp_path):
    """A01：子完成后父审批可查询、可恢复，Journal 不提前写最终消息。"""
    components, fake, _, service = stack(tmp_path, ParentApprovalClient())
    scopes = SCOPES | {"watchlist:write"}
    accepted, _, child = await started_child(service, fake, scopes=scopes)
    child["status"] = "success"
    interrupted = await service.status(accepted.run_id, scopes=frozenset({"*"}), **OWNER)
    assert interrupted.status == "interrupted"
    assert interrupted.waiting_reason == "approval_required"
    assert len(interrupted.pending_interactions) == 1
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 1
    assert set(fake.resume_calls[-1]["context"]["scopes"]) == scopes
    waiting = interrupted.pending_interactions[0]
    completed = await service.resume(
        accepted.run_id,
        ApprovalDecision(
            type="approve",
            arguments_hash=waiting["arguments_hash"],
            interrupt_id=waiting["interrupt_id"],
        ),
        scopes=scopes,
        **OWNER,
    )
    assert completed.status == "completed"
    assert fake.resume_calls[-1]["command"]["resume"]["parent-approval"]["decisions"] == [
        {"type": "approve"}
    ]
    execution = service.execution.get(accepted.run_id)
    assert execution["server_run_id"].startswith("server-resume-")
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 2
    assert (await service.status(accepted.run_id, **OWNER)).status == "completed"


@pytest.mark.asyncio
async def test_parallel_parent_delivery_and_journal_are_idempotent(tmp_path):
    """A04/A07：并发请求只提交一次父恢复，只写一条最终 Journal。"""
    components, fake, _, service = stack(tmp_path)
    accepted, _, child = await started_child(service, fake)
    child["status"] = "success"
    responses = await asyncio.gather(
        *(service.status(accepted.run_id, scopes=SCOPES, **OWNER) for _ in range(12))
    )
    assert all(r.status in {"waiting_child", "completed"} for r in responses)
    assert len(fake.resume_calls) == 1
    messages = components.conversation_repository.list_messages(accepted.conversation_id)
    assert len(messages) == 2 and [m.sequence for m in messages] == [1, 2]
    await asyncio.gather(
        *(
            asyncio.to_thread(
                components.conversation_repository.append_assistant_message,
                run_id=accepted.run_id,
                content=messages[-1].content,
            )
            for _ in range(12)
        )
    )
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 2


class LostReceiptClient(FakeDelegationClient):
    """模拟已提交回执丢失和未提交前崩溃两种不可混淆的边界。"""

    before_submit = False

    async def submit_resume(self, **kwargs):
        """生产客户端也不能因为 TimeoutError 就假定未执行。"""
        if self.before_submit:
            raise TimeoutError("crash before send")
        await super().submit_resume(**kwargs)
        raise TimeoutError("accepted but receipt lost")


@pytest.mark.parametrize("before_submit", [False, True])
@pytest.mark.asyncio
async def test_uncertain_resume_is_reconciled_never_blindly_retried(tmp_path, before_submit):
    """A05：相同操作对账，未知是否提交时保守等待，不产生第二次恢复。"""
    fake = LostReceiptClient()
    fake.before_submit = before_submit
    _, _, _, service = stack(tmp_path, fake)
    accepted, _, child = await started_child(service, fake)
    child["status"] = "success"
    first = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    assert first.waiting_reason == "submission_uncertain"
    for _ in range(3):
        result = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    assert len(fake.resume_calls) == (0 if before_submit else 1)
    assert result.status == ("waiting_child" if before_submit else "completed")
    if before_submit:
        assert result.waiting_reason == "submission_uncertain"
        assert (await service.cancel(accepted.run_id, **OWNER)).status == "cancellation_requested"


@pytest.mark.asyncio
async def test_active_turn_guard_cancel_and_clean_thread(tmp_path):
    """A11：等待期间的新消息不污染检查点，停止确认后换干净线程继续会话。"""
    components, fake, _, service = stack(tmp_path)
    accepted, _, _ = await started_child(service, fake)
    with pytest.raises(ConversationConflict):
        await service.start_turn(
            accepted.conversation_id,
            ConversationTurnRequest(message="unrelated"),
            scopes=SCOPES,
            idempotency_key="another",
            **OWNER,
        )
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 1
    cancelled = await service.cancel(accepted.run_id, **OWNER)
    assert cancelled.status == "cancelled"
    new = await service.start_turn(
        accepted.conversation_id,
        ConversationTurnRequest(message="new task"),
        scopes=SCOPES,
        idempotency_key="after-cancel",
        **OWNER,
    )
    assert new.thread_id != accepted.thread_id
    assert len(fake.resume_calls) == 0


@pytest.mark.asyncio
async def test_changed_action_and_expired_approval_cannot_resume(tmp_path):
    """A01/A03：旧动作摘要、编辑和过期决定均不能触发副作用执行。"""
    _, fake, _, service = stack(tmp_path, ParentApprovalClient())
    accepted, _, child = await started_child(service, fake, scopes=SCOPES | {"watchlist:write"})
    child["status"] = "success"
    waiting = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    with pytest.raises(ExecutionConflict):
        await service.resume(
            accepted.run_id,
            ApprovalDecision(type="approve", arguments_hash="0" * 64),
            scopes=SCOPES,
            **OWNER,
        )
    service._clock = lambda: datetime.now(UTC) + timedelta(days=1)
    expired = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    assert expired.waiting_reason == "approval_expired"
    assert len(fake.resume_calls) == 1
    assert waiting.pending_interactions[0]["arguments_hash"] == digest(
        {"name": "watchlist_add", "args": {"symbol": "AAPL", "note": "test"}}
    )


class ChainedClient(ParentApprovalClient):
    """父审批后再次委派，第二个子结果才产生最终回复。"""

    async def resume_run(self, **kwargs):
        """保留每次恢复身份，生成两个不同 handoff 实例。"""
        from financeclaw.modules.delegation import AgentHandoffV2

        payload = kwargs["command"]["resume"]
        if payload.get("delegation_id") == "second-child":
            self.resume_calls.append(kwargs)
            return {"messages": [AIMessage(content="both tasks completed")]}
        if "parent-approval" in payload:
            self.resume_calls.append(kwargs)
            context = kwargs["context"]
            handoff = AgentHandoffV2(
                handoff_id="second-child",
                parent_run_id=context["run_id"],
                parent_turn_id=context["turn_id"],
                conversation_id=context["conversation_id"],
                agent_id="market_research_agent",
                target_version="1.1.0",
                task="research MSFT",
            )
            return {"__interrupt__": [{"value": handoff.model_dump(mode="json")}]}
        return await super().resume_run(**kwargs)


@pytest.mark.asyncio
async def test_parent_approval_can_transition_into_another_delegation(tmp_path):
    """A02：审批恢复后的合法 handoff 继续等待；不会提前完成或复用旧 child。"""
    components, fake, _, service = stack(tmp_path, ChainedClient())
    scopes = SCOPES | {"watchlist:write"}
    accepted, initial, child = await started_child(service, fake, scopes=scopes)
    child["status"] = "success"
    waiting = await service.status(accepted.run_id, scopes=scopes, **OWNER)
    approval = waiting.pending_interactions[0]
    next_child = await service.resume(
        accepted.run_id,
        ApprovalDecision(
            type="approve",
            interrupt_id=approval["interrupt_id"],
            arguments_hash=approval["arguments_hash"],
        ),
        scopes=scopes,
        **OWNER,
    )
    assert next_child.status == "waiting_child"
    assert (
        next_child.output["delegation"]["child_run_id"]
        != initial.output["delegation"]["child_run_id"]
    )
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 1
    second = next(
        item
        for item in fake.runs.values()
        if item["metadata"].get("delegation_id") == "second-child"
    )
    second["status"] = "success"
    assert (await service.status(accepted.run_id, scopes=scopes, **OWNER)).status == "completed"
    assert len(fake.resume_calls) == 3
    components.database.close()
