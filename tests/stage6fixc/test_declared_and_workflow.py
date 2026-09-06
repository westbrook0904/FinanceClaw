"""声明式动作授权、Workflow 旧审批镜像和独立取消出口。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from financeclaw.application.execution_service import agent_snapshot
from financeclaw.application.interaction_service import InteractionService
from financeclaw.application.run_observation import observe_run
from financeclaw.kernel import ExecutionContext, WorkflowTarget
from financeclaw.modules.execution import ExecutionConflict
from financeclaw.modules.interactions import InteractionPoint, InteractionResponse
from financeclaw.modules.workflows import WorkflowConflict
from financeclaw.orchestration.agents import AgentProfileCatalog
from tests.stage4.support import workflow_arguments
from tests.stage6fix.test_execution_recovery import OWNER, stack
from tests.stage6fixc.test_interactions import SCOPES, question


@pytest.mark.asyncio
async def test_declared_approval_scope_does_not_expand_execution_scopes(tmp_path):
    """新增的决定权限仅用于审批，恢复上下文仍是原授权与当前执行权限的交集。"""
    components, fake, _, conversations = stack(tmp_path)
    point = InteractionPoint(
        point_id="publish", kind="approval", question="发布？", required_scope="reports:approve"
    )
    profile = components.agent_profiles.resolve("market_research_agent").model_copy(
        update={"interaction_points": (point,)}
    )
    context = ExecutionContext(
        **OWNER,
        run_id="declared",
        root_run_id="declared",
        turn_id="t",
        scopes=frozenset({"market:read"}),
    )
    execution = conversations.execution
    execution.register(
        "declared", agent_snapshot(profile, context, thread_id="t", input_hash="hash")
    )
    execution.prepare("initial", "declared", {})
    execution.bind("initial", "server")
    fake.runs["server"] = {"thread_id": "t", "status": "interrupted"}
    interactions = InteractionService(
        fake, execution, agent_profiles=AgentProfileCatalog((profile,))
    )
    native = question("native", "publish", "approval")
    native["value"]["action"] = {
        "report": "report-1",
        "evidence_hash": "a" * 64,
        "destination": "private",
    }
    row = await interactions.observe_agent(
        "declared",
        observe_run({"interrupts": [native]}),
        server_run_id="server",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    item = await interactions.public(row)
    response = InteractionResponse(
        revision=item["revision"],
        kind="approval",
        decision="approve",
        action_hash=item["action_hash"],
    )
    with pytest.raises(ExecutionConflict):
        await interactions.respond(
            item["interaction_id"],
            response,
            scopes=context.scopes,
            idempotency_key="approve",
            **OWNER,
        )
    await interactions.respond(
        item["interaction_id"],
        response,
        scopes=context.scopes | {"reports:approve", "unrelated:new"},
        idempotency_key="approve",
        **OWNER,
    )
    assert set(fake.resume_calls[-1]["context"]["scopes"]) == {"market:read"}
    assert (
        fake.resume_calls[-1]["command"]["resume"]["native"]["action_hash"] == item["action_hash"]
    )


@pytest.mark.parametrize("kind", ["approve", "reject", "cancel"])
@pytest.mark.asyncio
async def test_workflow_interaction_and_original_approval_have_one_decision(tmp_path, kind):
    """C02/C04：Workflow 决定镜像与操作准备原子一致，独立 Workflow 也能安全取消。"""
    _, fake, delegation, _ = stack(tmp_path)
    workflow = delegation.workflow_service
    accepted = await workflow.start(
        WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("C")),
        scopes=SCOPES,
        idempotency_key="workflow-c",
        **OWNER,
    )
    fake.interrupt_workflow(accepted.run_id)
    owner = workflow.execution.get(accepted.run_id)
    original = fake.runs[owner["server_run_id"]]
    original["interrupts"][0]["id"] = "workflow-native"
    waiting = await workflow.status(accepted.run_id, scopes=SCOPES, **OWNER)
    item = waiting.pending_interactions[0]
    if kind == "cancel":
        assert (await workflow.cancel(accepted.run_id, **OWNER)).status == "cancelled"
        rebound = workflow.repository.bind_server_run(
            accepted.run_id, owner["server_run_id"], "running"
        )
        assert rebound.status.value == "cancelled"
        assert (await workflow.status(accepted.run_id, **OWNER)).status == "cancelled"
        assert not fake.resume_calls
        expected = "cancelled"
    else:
        for _ in range(2):
            await workflow.interactions.respond(
                item["interaction_id"],
                InteractionResponse(
                    revision=item["revision"],
                    kind="approval",
                    decision=kind,
                    action_hash=item["action_hash"],
                    reason="用户决定",
                ),
                scopes=SCOPES,
                idempotency_key="decision",
                **OWNER,
            )
        assert len(fake.resume_calls) == 1
        expected = "approved" if kind == "approve" else "rejected"
        with pytest.raises(WorkflowConflict, match="stale"):
            await workflow._record_interrupt(
                workflow.repository.get_owned(accepted.run_id, **OWNER),
                original,
                server_run_id=owner["server_run_id"],
            )
    approval = workflow.repository.get_approval(accepted.run_id, approval_id=item["approval_id"])
    assert approval.status.value == expected
    row = workflow.interactions.repository.get_owned(
        item["interaction_id"], **OWNER, now=workflow._now()
    )
    assert row["status"] == ("resolved" if expected == "approved" else expected)
    if kind != "cancel":
        assert row["operation_id"] and approval.decision_reason == "用户决定"


@pytest.mark.parametrize("schema", [{"$ref": "https://invalid.example/schema"}, {"type": "array"}])
def test_interaction_schema_must_be_local_and_object_shaped(schema):
    """输入 Schema 来自发布声明且不触发远程解析。"""
    with pytest.raises(ValidationError):
        InteractionPoint(
            point_id="details", kind="input", question="资料？", response_schema=schema
        )
