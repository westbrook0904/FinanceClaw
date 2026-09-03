"""`test_workflow_service_api` 模块提供`stage4`相关能力。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from financeclaw.application import (
    IdempotencyConflict,
    RunNotFound,
    RunService,
    TargetResolver,
    WorkflowApprovalExpired,
    WorkflowService,
)
from financeclaw.interfaces.http import create_app
from financeclaw.interfaces.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.kernel import ApprovalDecision, WorkflowTarget
from financeclaw.modules.audit import AuditEventType
from financeclaw.modules.workflows import (
    SqlAlchemyWorkflowRepository,
    WorkflowApprovalStatus,
    WorkflowCatalog,
    WorkflowStatus,
)
from financeclaw.orchestration.agents import AgentProfileCatalog
from financeclaw.orchestration.tools import ToolCatalog, default_local_tools

from .support import workflow_arguments, workflow_stack

SCOPES = frozenset({"portfolio:review", "market:read", "workflows:approve"})


@pytest.mark.asyncio
async def test_durable_idempotency_interrupt_restart_approve_and_audit(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and repository and catalog and audit
    # and fake and clock and
    # service，供后续步骤使用。
    database, repository, catalog, audit, fake, clock, service = workflow_stack(
        tmp_path / "restart.db"
    )
    # 准备 target，供后续步骤使用。
    target = WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments())
    # 准备 accepted，供后续步骤使用。
    accepted = await service.start(
        target,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="review-1",
    )
    # 准备 replay，供后续步骤使用。
    replay = await service.start(
        target,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="review-1",
    )
    # 继续执行前验证内部不变量。
    assert replay.run_id == accepted.run_id
    # 继续执行前验证内部不变量。
    assert replay.idempotent_replay
    # 继续执行前验证内部不变量。
    assert len(fake.create_calls) == 1
    # 继续执行前验证内部不变量。
    assert fake.create_calls[0]["assistant_id"] == "portfolio_review_v1"
    # 继续执行前验证内部不变量。
    assert fake.create_calls[0]["metadata"]["workflow_version"] == "1.0.0"

    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(IdempotencyConflict):
        await service.start(
            WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("Changed")),
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=SCOPES,
            idempotency_key="review-1",
        )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(RunNotFound):
        await service.status(accepted.run_id, tenant_id="tenant-b", subject_id="subject-a")

    # 准备 approval_payload，供后续步骤使用。
    approval_payload = fake.interrupt(accepted.run_id)
    # 准备 interrupted，供后续步骤使用。
    interrupted = await service.status(
        accepted.run_id, tenant_id="tenant-a", subject_id="subject-a"
    )
    # 继续执行前验证内部不变量。
    assert interrupted.status == "interrupted"
    # 继续执行前验证内部不变量。
    assert interrupted.output["approval"]["arguments_hash"] == approval_payload["arguments_hash"]

    # 准备 old_release，供后续步骤使用。
    old_release = catalog[("portfolio_review", "1.0.0")]
    # 准备 redeployed_catalog，供后续步骤使用。
    redeployed_catalog = WorkflowCatalog(
        (
            replace(old_release, status=WorkflowStatus.DEPRECATED),
            replace(
                old_release,
                version="2.0.0",
                assistant_id="portfolio_review_v2",
                deployment_revision="portfolio-review-v2/revision-1",
            ),
        )
    )
    # 继续执行前验证内部不变量。
    assert redeployed_catalog.resolve("portfolio_review").version == "2.0.0"
    # 准备 restarted_repository，供后续步骤使用。
    restarted_repository = SqlAlchemyWorkflowRepository(database.session_factory)
    # 准备 restarted，供后续步骤使用。
    restarted = WorkflowService(fake, restarted_repository, redeployed_catalog, audit, clock=clock)
    # 准备 completed，供后续步骤使用。
    completed = await restarted.resume(
        accepted.run_id,
        ApprovalDecision(type="approve", arguments_hash=approval_payload["arguments_hash"]),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )
    # 继续执行前验证内部不变量。
    assert completed.status == "completed"
    # 继续执行前验证内部不变量。
    assert completed.output["artifact"]["artifact_id"] == "artifact-test-report"
    # 准备 persisted，供后续步骤使用。
    persisted = restarted_repository.get_owned(accepted.run_id, "tenant-a", "subject-a")
    # 继续执行前验证内部不变量。
    assert persisted.workflow_version == "1.0.0"
    # 继续执行前验证内部不变量。
    assert persisted.assistant_id == "portfolio_review_v1"
    # 继续执行前验证内部不变量。
    assert persisted.artifact_refs == ("artifact-test-report",)
    # 准备 approval，供后续步骤使用。
    approval = restarted_repository.get_approval(accepted.run_id)
    # 继续执行前验证内部不变量。
    assert approval.status is WorkflowApprovalStatus.APPROVED
    # 继续执行前验证内部不变量。
    assert [record.event_type for record in audit.records()] == [
        AuditEventType.WORKFLOW_STARTED,
        AuditEventType.WORKFLOW_INTERRUPTED,
        AuditEventType.WORKFLOW_APPROVED,
        AuditEventType.WORKFLOW_COMPLETED,
    ]
    # 前置条件满足后调用 close。
    database.close()


@pytest.mark.asyncio
async def test_run_and_approval_timeouts_are_durable_terminal_states(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and repository and _ and audit and
    # fake and clock and
    # service，供后续步骤使用。
    database, repository, _, audit, fake, clock, service = workflow_stack(tmp_path / "timeouts.db")
    # 准备 first，供后续步骤使用。
    first = await service.start(
        WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("run")),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="run-timeout",
    )
    # 使用最新结果更新 clock.value。
    clock.value += timedelta(seconds=61)
    # 准备 timed_out，供后续步骤使用。
    timed_out = await service.status(first.run_id, tenant_id="tenant-a", subject_id="subject-a")
    # 继续执行前验证内部不变量。
    assert timed_out.status == "failed"

    # 准备 clock.value，供后续步骤使用。
    clock.value = datetime.now(UTC)
    # 准备 second，供后续步骤使用。
    second = await service.start(
        WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("approval")),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="approval-timeout",
    )
    # 准备 payload，供后续步骤使用。
    payload = fake.interrupt(second.run_id)
    # 等待 status 完成后再推进流程。
    await service.status(second.run_id, tenant_id="tenant-a", subject_id="subject-a")
    # 使用最新结果更新 clock.value。
    clock.value += timedelta(seconds=61)
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(WorkflowApprovalExpired):
        await service.resume(
            second.run_id,
            ApprovalDecision(type="approve", arguments_hash=payload["arguments_hash"]),
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=SCOPES,
        )
    # 继续执行前验证内部不变量。
    assert repository.get_owned(second.run_id, "tenant-a", "subject-a").status == "failed"
    # 继续执行前验证内部不变量。
    assert repository.get_approval(second.run_id).status is WorkflowApprovalStatus.EXPIRED
    # 继续执行前验证内部不变量。
    assert [record.decision for record in audit.records()].count("approval_timeout") == 1
    # 前置条件满足后调用 close。
    database.close()


@pytest.mark.asyncio
async def test_workflow_http_contract_and_generic_target_routing(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and _ and catalog and audit and fake
    # and _ and
    # workflow_service，供后续步骤使用。
    database, _, catalog, audit, fake, _, workflow_service = workflow_stack(tmp_path / "api.db")
    # 准备 resolver，供后续步骤使用。
    resolver = TargetResolver(
        tool_catalog=ToolCatalog(default_local_tools()),
        agent_profiles=AgentProfileCatalog(()),
        workflow_catalog=catalog,
    )
    # 准备 run_service，供后续步骤使用。
    run_service = RunService(fake, resolver)
    # 准备 app，供后续步骤使用。
    app = create_app(
        run_service=run_service,
        authenticator=StaticBearerAuthenticator(
            {
                "token-a": AuthenticatedPrincipal(
                    tenant_id="tenant-a",
                    subject_id="subject-a",
                    scopes=SCOPES | {"internal:invoke"},
                ),
                "token-b": AuthenticatedPrincipal(
                    tenant_id="tenant-b", subject_id="subject-b", scopes=SCOPES
                ),
            }
        ),
        workflow_service=workflow_service,
    )
    # 准备 transport，供后续步骤使用。
    transport = httpx.ASGITransport(app=app)
    # 准备 headers，供后续步骤使用。
    headers = {"Authorization": "Bearer token-a", "Idempotency-Key": "http-review"}
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post(
            "/v1/workflows/portfolio_review/runs",
            json={"arguments": workflow_arguments()},
            headers=headers,
        )
        run_id = started.json()["run_id"]
        approval = fake.interrupt(run_id)
        interrupted = await client.get(
            f"/v1/runs/{run_id}", headers={"Authorization": "Bearer token-a"}
        )
        hidden = await client.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer token-b"})
        completed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"type": "approve", "arguments_hash": approval["arguments_hash"]},
            headers={"Authorization": "Bearer token-a"},
        )
        generic = await client.post(
            "/v1/runs",
            json={
                "message": "published portfolio review",
                "target": {
                    "kind": "workflow",
                    "workflow_id": "portfolio_review",
                    "arguments": workflow_arguments("Generic"),
                },
            },
            headers={
                "Authorization": "Bearer token-a",
                "Idempotency-Key": "generic-review",
            },
        )
        health = await client.get("/health")

    # 继续执行前验证内部不变量。
    assert started.status_code == 202
    # 继续执行前验证内部不变量。
    assert started.json()["target_kind"] == "workflow"
    # 继续执行前验证内部不变量。
    assert interrupted.json()["status"] == "interrupted"
    # 继续执行前验证内部不变量。
    assert hidden.status_code == 404
    # 继续执行前验证内部不变量。
    assert completed.json()["status"] == "completed"
    # 继续执行前验证内部不变量。
    assert generic.status_code == 202
    # 继续执行前验证内部不变量。
    assert generic.json()["target_kind"] == "workflow"
    # 继续执行前验证内部不变量。
    assert health.json() == {"status": "ok", "stage": "5"}
    # 继续执行前验证内部不变量。
    assert AuditEventType.WORKFLOW_COMPLETED in [record.event_type for record in audit.records()]
    # 前置条件满足后调用 close。
    database.close()
