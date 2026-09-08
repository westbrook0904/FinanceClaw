"""独立旧生产者进程：产生静止旧根后退出，不与新 Coordinator 同时驱动。"""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from financeclaw.coordination.application.conversation_runs import ConversationRunService
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


async def main():
    """正式旧服务、正式发布图与合成模型；输出只保存本次合成根 ID。"""
    settings = FinanceClawSettings(_env_file=None)
    services = build_coordination(settings)
    legacy = ConversationRunService(
        services.client,
        services.resources.conversation_repository,
        services.releases.agent_profiles,
        delegation_service=services.delegations,
    )
    result = {}
    try:
        for case, message in (
            ("root", "calculate"),
            ("child", "/agent market_research_agent synthetic research"),
            (
                "workflow",
                '/workflow portfolio_review {"portfolio_name":"synthetic",'
                '"positions":[{"symbol":"AAPL","quantity":1,"cost_basis":1}]}',
            ),
        ):
            conversation = legacy.repository.create_conversation(
                tenant_id=settings.bff_tenant_id,
                subject_id=settings.bff_subject_id,
                agent_id="finance_agent",
                agent_profile_version="1.4.0",
            )
            accepted = await legacy.start_turn(
                conversation.conversation_id,
                ConversationTurnRequest(message=message),
                tenant_id=settings.bff_tenant_id,
                subject_id=settings.bff_subject_id,
                scopes=settings.bff_scopes,
                idempotency_key="legacy-" + case,
            )
            for _ in range(600):
                if case != "root":
                    await legacy.status(
                        accepted.run_id,
                        tenant_id=settings.bff_tenant_id,
                        subject_id=settings.bff_subject_id,
                        scopes=settings.bff_scopes,
                    )
                    with services.resources.database.session_factory() as session:
                        waiting = session.scalar(
                            select(PendingInteractionRow.interaction_id).where(
                                PendingInteractionRow.root_run_id == accepted.run_id
                            )
                        )
                    if waiting:
                        break
                else:
                    execution = legacy.execution.get(accepted.run_id)
                    run = await services.client.get_run(
                        thread_id=execution["snapshot"]["thread_id"],
                        run_id=execution["server_run_id"],
                    )
                    if run["status"] in {"success", "completed"}:
                        break
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("legacy synthetic task did not reach its stopped position")
            result[case] = accepted.run_id
        Path("legacy-roots.json").write_text(json.dumps(result))
    finally:
        services.resources.database.close()


if __name__ == "__main__":
    asyncio.run(main())
