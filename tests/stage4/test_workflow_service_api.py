from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from financeclaw.agents import AgentProfileCatalog
from financeclaw.api import create_app
from financeclaw.api.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.application import (
    IdempotencyConflict,
    RunNotFound,
    RunService,
    TargetResolver,
    WorkflowApprovalExpired,
    WorkflowService,
)
from financeclaw.audit import AuditEventType
from financeclaw.contracts import ApprovalDecision, WorkflowTarget
from financeclaw.tools import ToolCatalog, default_local_tools
from financeclaw.workflows import (
    SqlAlchemyWorkflowRepository,
    WorkflowApprovalStatus,
    WorkflowCatalog,
    WorkflowStatus,
)

from .support import workflow_arguments, workflow_stack

SCOPES = frozenset({"portfolio:review", "market:read", "workflows:approve"})


@pytest.mark.asyncio
async def test_durable_idempotency_interrupt_restart_approve_and_audit(tmp_path: Path) -> None:
    database, repository, catalog, audit, fake, clock, service = workflow_stack(
        tmp_path / "restart.db"
    )
    target = WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments())
    accepted = await service.start(
        target,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="review-1",
    )
    replay = await service.start(
        target,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="review-1",
    )
    assert replay.run_id == accepted.run_id
    assert replay.idempotent_replay
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0]["assistant_id"] == "portfolio_review_v1"
    assert fake.create_calls[0]["metadata"]["workflow_version"] == "1.0.0"

    with pytest.raises(IdempotencyConflict):
        await service.start(
            WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("Changed")),
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=SCOPES,
            idempotency_key="review-1",
        )
    with pytest.raises(RunNotFound):
        await service.status(accepted.run_id, tenant_id="tenant-b", subject_id="subject-a")

    approval_payload = fake.interrupt(accepted.run_id)
    interrupted = await service.status(
        accepted.run_id, tenant_id="tenant-a", subject_id="subject-a"
    )
    assert interrupted.status == "interrupted"
    assert interrupted.output["approval"]["arguments_hash"] == approval_payload["arguments_hash"]

    old_release = catalog[("portfolio_review", "1.0.0")]
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
    assert redeployed_catalog.resolve("portfolio_review").version == "2.0.0"
    restarted_repository = SqlAlchemyWorkflowRepository(database.session_factory)
    restarted = WorkflowService(fake, restarted_repository, redeployed_catalog, audit, clock=clock)
    completed = await restarted.resume(
        accepted.run_id,
        ApprovalDecision(type="approve", arguments_hash=approval_payload["arguments_hash"]),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )
    assert completed.status == "completed"
    assert completed.output["artifact"]["artifact_id"] == "artifact-test-report"
    persisted = restarted_repository.get_owned(accepted.run_id, "tenant-a", "subject-a")
    assert persisted.workflow_version == "1.0.0"
    assert persisted.assistant_id == "portfolio_review_v1"
    assert persisted.artifact_refs == ("artifact-test-report",)
    approval = restarted_repository.get_approval(accepted.run_id)
    assert approval.status is WorkflowApprovalStatus.APPROVED
    assert [record.event_type for record in audit.records()] == [
        AuditEventType.WORKFLOW_STARTED,
        AuditEventType.WORKFLOW_INTERRUPTED,
        AuditEventType.WORKFLOW_APPROVED,
        AuditEventType.WORKFLOW_COMPLETED,
    ]
    database.close()


@pytest.mark.asyncio
async def test_run_and_approval_timeouts_are_durable_terminal_states(tmp_path: Path) -> None:
    database, repository, _, audit, fake, clock, service = workflow_stack(tmp_path / "timeouts.db")
    first = await service.start(
        WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("run")),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="run-timeout",
    )
    clock.value += timedelta(seconds=61)
    timed_out = await service.status(first.run_id, tenant_id="tenant-a", subject_id="subject-a")
    assert timed_out.status == "failed"

    clock.value = datetime.now(UTC)
    second = await service.start(
        WorkflowTarget(workflow_id="portfolio_review", arguments=workflow_arguments("approval")),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="approval-timeout",
    )
    payload = fake.interrupt(second.run_id)
    await service.status(second.run_id, tenant_id="tenant-a", subject_id="subject-a")
    clock.value += timedelta(seconds=61)
    with pytest.raises(WorkflowApprovalExpired):
        await service.resume(
            second.run_id,
            ApprovalDecision(type="approve", arguments_hash=payload["arguments_hash"]),
            tenant_id="tenant-a",
            subject_id="subject-a",
            scopes=SCOPES,
        )
    assert repository.get_owned(second.run_id, "tenant-a", "subject-a").status == "failed"
    assert repository.get_approval(second.run_id).status is WorkflowApprovalStatus.EXPIRED
    assert [record.decision for record in audit.records()].count("approval_timeout") == 1
    database.close()


@pytest.mark.asyncio
async def test_workflow_http_contract_and_generic_target_routing(tmp_path: Path) -> None:
    database, _, catalog, audit, fake, _, workflow_service = workflow_stack(tmp_path / "api.db")
    resolver = TargetResolver(
        tool_catalog=ToolCatalog(default_local_tools()),
        agent_profiles=AgentProfileCatalog(()),
        workflow_catalog=catalog,
    )
    run_service = RunService(fake, resolver)
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
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer token-a", "Idempotency-Key": "http-review"}
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

    assert started.status_code == 202
    assert started.json()["target_kind"] == "workflow"
    assert interrupted.json()["status"] == "interrupted"
    assert hidden.status_code == 404
    assert completed.json()["status"] == "completed"
    assert generic.status_code == 202
    assert generic.json()["target_kind"] == "workflow"
    assert health.json() == {"status": "ok", "stage": "4"}
    assert AuditEventType.WORKFLOW_COMPLETED in [record.event_type for record in audit.records()]
    database.close()
