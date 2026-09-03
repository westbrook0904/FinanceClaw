"""提供 workflow smoke 运维命令的可调用入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from pydantic import SecretStr

from financeclaw.application import WorkflowService
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.clients import LangGraphAgentServerClient
from financeclaw.kernel import ApprovalDecision, WorkflowTarget
from financeclaw.modules.audit import AuditEventType, SqlAlchemyAuditRepository
from financeclaw.modules.workflows import SqlAlchemyWorkflowRepository


async def _wait_for_interrupt(
    service: WorkflowService,
    run_id: str,
    *,
    tenant_id: str,
    subject_id: str,
    timeout_seconds: float,
):
    """轮询工作流，直到出现审批中断；超时或提前终结均视为失败。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        status = await service.status(run_id, tenant_id=tenant_id, subject_id=subject_id)
        if status.status == "interrupted":
            return status
        if status.status in {"completed", "rejected", "failed"}:
            raise RuntimeError(
                f"workflow smoke terminated before approval: {status.model_dump(mode='json')!r}"
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("workflow smoke did not reach its approval checkpoint")
        await asyncio.sleep(0.2)


async def probe_workflow(
    *,
    url: str,
    database_url: str,
    artifact_root: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """启动投资组合复核工作流，并验证中断审批与终态输出。"""
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=SecretStr(database_url),
        artifact_root=artifact_root,
    )
    components = build_components(settings, enable_persistence=True)
    if components.database is None:
        raise RuntimeError("application database is unavailable")
    if not isinstance(components.workflow_repository, SqlAlchemyWorkflowRepository):
        raise RuntimeError("persistent WorkflowRepository is unavailable")
    if components.workflow_catalog is None:
        raise RuntimeError("published WorkflowCatalog is unavailable")
    if not isinstance(components.audit, SqlAlchemyAuditRepository):
        raise RuntimeError("persistent AuditRepository is unavailable")
    client = LangGraphAgentServerClient(url=url)
    service = WorkflowService(
        client,
        components.workflow_repository,
        components.workflow_catalog,
        components.audit,
    )
    smoke_id = uuid4().hex
    tenant_id = f"stage4-smoke-{smoke_id}"
    subject_id = f"stage4-smoke-{smoke_id}"
    scopes = frozenset({"portfolio:review", "market:read", "workflows:approve"})
    try:
        accepted = await service.start(
            WorkflowTarget(
                workflow_id="portfolio_review",
                version="1.0.0",
                arguments={
                    "portfolio_name": "Stage-4 smoke portfolio",
                    "positions": [
                        {"symbol": "AAPL", "quantity": "2", "cost_basis": "80"},
                        {"symbol": "MSFT", "quantity": "1", "cost_basis": "90"},
                    ],
                    "max_snapshot_age_hours": 48,
                },
            ),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            idempotency_key=f"stage4-workflow-{smoke_id}",
        )
        interrupted = await _wait_for_interrupt(
            service,
            accepted.run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            timeout_seconds=timeout_seconds,
        )
        approval = interrupted.output["approval"]
        completed = await service.resume(
            accepted.run_id,
            ApprovalDecision(type="approve", arguments_hash=str(approval["arguments_hash"])),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
        )
        if completed.status != "completed" or completed.output is None:
            raise RuntimeError("approved portfolio workflow did not complete")
        artifact = completed.output.get("artifact")
        if not isinstance(artifact, dict) or not artifact.get("artifact_id"):
            raise RuntimeError("portfolio workflow did not publish its report artifact")

        restarted = WorkflowService(
            client,
            SqlAlchemyWorkflowRepository(components.database.session_factory),
            components.workflow_catalog,
            components.audit,
        )
        recovered = await restarted.status(
            accepted.run_id, tenant_id=tenant_id, subject_id=subject_id
        )
        events = components.audit.records(tenant_id=tenant_id, subject_id=subject_id)
        required_events = {
            AuditEventType.WORKFLOW_STARTED,
            AuditEventType.WORKFLOW_INTERRUPTED,
            AuditEventType.WORKFLOW_APPROVED,
            AuditEventType.WORKFLOW_COMPLETED,
        }
        if not required_events.issubset({record.event_type for record in events}):
            raise RuntimeError("workflow lifecycle Audit records are incomplete")
        return {
            "run_id": accepted.run_id,
            "thread_id": accepted.thread_id,
            "workflow_version": completed.output["workflow_version"],
            "interrupted": True,
            "approved": True,
            "recovered_status": recovered.status,
            "artifact_id": artifact["artifact_id"],
            "audit_count": len(events),
        }
    finally:
        if components.database is not None:
            components.database.close()


def main() -> None:
    """解析命令行参数，执行 workflow smoke 操作并输出结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    parser.add_argument(
        "--database-url",
        default="sqlite+pysqlite:///./.financeclaw/financeclaw.db",
    )
    parser.add_argument("--artifact-root", default=".financeclaw/artifacts")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    result = asyncio.run(
        probe_workflow(
            url=args.url,
            database_url=args.database_url,
            artifact_root=args.artifact_root,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
