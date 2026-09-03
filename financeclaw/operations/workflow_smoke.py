"""固定 Workflow 的独立运行冒烟命令（Stage-4）。

基于真实持久化与本地 Agent Server 运行 ``portfolio_review`` 工作流（独立
thread）：等待 HITL 审批中断、携带 arguments_hash 批准、校验报告工件与
工作流生命周期 Audit，并用全新 WorkflowService（新仓储实例）验证进程内
业务恢复。运行方式：``python -m financeclaw.operations.workflow_smoke``。
"""

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
    """轮询工作流 run 状态直至到达审批中断点。

    Args:
        service: 工作流服务，用于查询 run 状态。
        run_id: 待轮询的 run ID。
        tenant_id: 租户 ID。
        subject_id: 主体 ID。
        timeout_seconds: 轮询超时秒数。

    Returns:
        携带审批负载（``output["approval"]``）的中断状态响应。

    Raises:
        RuntimeError: run 在审批前以 completed/rejected/failed 终态结束。
        TimeoutError: 超时仍未到达审批检查点。

    """
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
    """执行一次工作流冒烟：启动、审批、工件、审计与业务恢复校验。

    Args:
        url: Agent Server 基础地址。
        database_url: 业务数据库连接串。
        artifact_root: 工件存储根目录。
        timeout_seconds: 状态轮询超时秒数。

    Returns:
        含 run/thread ID、工作流版本、恢复状态、工件 ID 与审计数量的摘要。

    Raises:
        RuntimeError: 持久化组件缺失、审批后未完成、未产出报告工件或审计不全。
        TimeoutError: 工作流未在超时窗口内到达审批检查点。

    """
    # 1. 构建离线模型、启用持久化的组件装配，并确认数据库与各仓储可用。
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
    # 2. 构造工作流服务，并用随机 smoke_id 生成隔离的租户、主体与审批作用域。
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
        # 3. 启动 portfolio_review 工作流并等待其进入审批中断。
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
        # 4. 携带中断负载中的 arguments_hash 批准审批并等待完成。
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
        # 5. 校验报告工件已随完成输出发布。
        artifact = completed.output.get("artifact")
        if not isinstance(artifact, dict) or not artifact.get("artifact_id"):
            raise RuntimeError("portfolio workflow did not publish its report artifact")
        # 6. 以全新仓储实例重建 WorkflowService，模拟进程内业务恢复并复核状态。
        restarted = WorkflowService(
            client,
            SqlAlchemyWorkflowRepository(components.database.session_factory),
            components.workflow_catalog,
            components.audit,
        )
        recovered = await restarted.status(
            accepted.run_id, tenant_id=tenant_id, subject_id=subject_id
        )
        # 7. 校验工作流生命周期审计事件齐全并汇总冒烟结果。
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
    """解析命令行参数并执行一次工作流冒烟，输出 JSON 摘要。"""
    # 1. 解析命令行参数（Server 地址、数据库、工件目录与轮询超时）。
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    parser.add_argument(
        "--database-url",
        default="sqlite+pysqlite:///./.financeclaw/financeclaw.db",
    )
    parser.add_argument("--artifact-root", default=".financeclaw/artifacts")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    # 2. 执行冒烟探针。
    result = asyncio.run(
        probe_workflow(
            url=args.url,
            database_url=args.database_url,
            artifact_root=args.artifact_root,
            timeout_seconds=args.timeout_seconds,
        )
    )
    # 3. 打印 JSON 结果。
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
