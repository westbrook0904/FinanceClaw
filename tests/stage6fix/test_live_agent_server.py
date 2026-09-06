"""可选真实 Agent Server 验收：显式启用后启动隔离本地服务，不连接线上供应商。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from financeclaw.application import ConversationService, DelegationService, WorkflowService
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.clients.agent_server import LangGraphAgentServerClient
from financeclaw.kernel import ApprovalDecision, ConversationTurnRequest, WorkflowTarget
from tests.stage4.support import workflow_arguments
from tests.stage6fix.test_execution_recovery import OWNER

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCECLAW_RUN_AGENT_SERVER_TESTS") != "1",
    reason="opt in to an isolated local Agent Server process",
)
SCOPES = frozenset(
    {"market:read", "watchlist:write", "portfolio:review", "workflows:approve", "artifacts:read"}
)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """临时 SQLite 和真实 checkpoint 服务；退出时只停止本测试启动的子进程。"""
    directory = tmp_path_factory.mktemp("stage6fix-live")
    environment_patch = pytest.MonkeyPatch()
    environment_patch.setenv("NO_PROXY", "127.0.0.1,localhost")
    environment_patch.setenv("no_proxy", "127.0.0.1,localhost")
    root = Path(__file__).resolve().parents[2]
    database = f"sqlite+pysqlite:///{directory / 'business.db'}"
    artifacts = str(directory / "artifacts")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "TMPDIR", "VIRTUAL_ENV"}
    }
    environment.update(
        STAGE6FIX_TEST_DATABASE=database,
        STAGE6FIX_TEST_ARTIFACTS=artifacts,
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        LANGGRAPH_AUTH_TYPE="noop",
        LANGGRAPH_API_DO_NOT_TRACK="true",
        PYTHONPATH=str(root),
    )
    graphs = {
        "finance_agent_v1_1_0": "finance_agent",
        "market_research_agent_v1_1_0": "market_research_agent",
        "portfolio_review_v1": "portfolio_review_v1",
    }
    config = directory / "langgraph.json"
    config.write_text(
        json.dumps(
            {
                "dependencies": [str(root)],
                "graphs": {
                    key: f"{root / 'tests/stage6fix/server_fixture.py'}:{value}"
                    for key, value in graphs.items()
                },
                "env": {},
            }
        )
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log_path = directory / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                str(Path(sys.executable).parent / "langgraph"),
                "dev",
                "--config",
                str(config),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-browser",
                "--no-reload",
                "--allow-blocking",
                "--server-log-level",
                "WARNING",
            ],
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Agent Server exited: " + log_path.read_text()[-8000:])
                try:
                    response = httpx.get(url + "/ok", timeout=1, trust_env=False)
                    if response.is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Agent Server did not start: " + log_path.read_text()[-8000:])
            yield url, database, artifacts, log_path
        finally:
            environment_patch.undo()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def live_stack(live_server):
    """BFF 与 Server 共享业务数据库，但使用真实 HTTP 和独立进程检查点。"""
    url, database, artifacts, _ = live_server
    components = build_components(
        FinanceClawSettings(
            _env_file=None,
            environment="test",
            offline_model=True,
            database_url=SecretStr(database),
            artifact_root=artifacts,
        ),
        enable_persistence=True,
    )
    client = LangGraphAgentServerClient(url=url)
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
    service = ConversationService(
        client,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=delegation,
    )
    return components, client, workflow, service


async def until_status(service, run_id, expected, **kwargs):
    """有限轮询，以业务状态而不是流结束或睡眠长度判断验收结果。"""
    for _ in range(150):
        result = await service.status(run_id, **OWNER, **kwargs)
        if result.status == expected:
            return result
        assert result.status != "failed", result
        await asyncio.sleep(0.1)
    pytest.fail(f"run did not reach {expected}: {result}")


@pytest.mark.asyncio
async def test_real_parent_child_hitl_attempt_ownership(live_server):
    """A01/A04/A06：真实子运行完成、父审批、新 Server Run 和旧检查点归属。"""
    components, client, _, service = live_stack(live_server)
    conversation = await service.create(**OWNER)
    accepted = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="research then watchlist"),
        scopes=SCOPES,
        idempotency_key="live-parent",
        **OWNER,
    )
    initial = service.execution.get(accepted.run_id)["server_run_id"]
    approval = await until_status(service, accepted.run_id, "interrupted", scopes=SCOPES)
    assert approval.waiting_reason == "approval_required"
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 1
    pending = approval.pending_interactions[0]
    resumed = await service.resume(
        accepted.run_id,
        ApprovalDecision(
            type="approve",
            arguments_hash=pending["arguments_hash"],
            interrupt_id=pending["interrupt_id"],
        ),
        scopes=SCOPES,
        **OWNER,
    )
    final = (
        resumed
        if resumed.status == "completed"
        else await until_status(service, accepted.run_id, "completed", scopes=SCOPES)
    )
    assert final.status == "completed"
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 2
    old = await client.get_run(thread_id=accepted.thread_id, run_id=initial)
    assert old["status"] == "interrupted" and old["interrupts"]
    execution = service.execution.get(accepted.run_id)
    assert execution["server_run_id"] != initial
    assert execution["tool_calls"] >= 3 and execution["model_calls"] >= 5
    operations = service.execution.operations_for_run(accepted.run_id)
    assert len(operations) == 3 and len({item["server_run_id"] for item in operations}) == 3
    components.database.close()


@pytest.mark.asyncio
async def test_real_workflow_approval_resume_uses_new_attempt(live_server):
    """已有 Workflow 在真实 Server 上批准发布，并把所有读写计入持久预算。"""
    components, _, workflow, _ = live_stack(live_server)
    accepted = await workflow.start(
        WorkflowTarget(
            workflow_id="portfolio_review",
            arguments={
                **workflow_arguments("real-server"),
                "max_snapshot_age_hours": 24,
            },
        ),
        scopes=SCOPES,
        idempotency_key="live-workflow",
        **OWNER,
    )
    waiting = await until_status(workflow, accepted.run_id, "interrupted")
    approval = waiting.pending_interactions[0]
    resumed = await workflow.resume(
        accepted.run_id,
        ApprovalDecision(
            type="approve",
            arguments_hash=approval["arguments_hash"],
            interrupt_id=approval["interrupt_id"],
        ),
        scopes=SCOPES,
        **OWNER,
    )
    result = (
        resumed
        if resumed.status == "completed"
        else await until_status(workflow, accepted.run_id, "completed")
    )
    assert result.output["artifact"]["artifact_id"]
    assert workflow.execution.get(accepted.run_id)["tool_calls"] >= 2
    components.database.close()


@pytest.mark.asyncio
async def test_real_workflow_business_failure_is_not_an_interrupt(live_server):
    """Server success 与领域 failed 分层：过期演示行情返回失败事实，不寻找审批。"""
    components, _, workflow, _ = live_stack(live_server)
    accepted = await workflow.start(
        WorkflowTarget(
            workflow_id="portfolio_review",
            arguments={
                **workflow_arguments("stale evidence"),
                "max_snapshot_age_hours": 1,
            },
        ),
        scopes=SCOPES,
        idempotency_key="live-stale",
        **OWNER,
    )
    result = await until_status(workflow, accepted.run_id, "failed")
    assert result.output["error"] and not result.pending_interactions
    assert result.output["artifact"] is None
    components.database.close()


@pytest.mark.asyncio
async def test_real_cancel_retains_checkpoint_and_rotates_thread(live_server):
    """A11：取消停止确认使用真实 Server；下个用户任务不复用挂起检查点。"""
    components, client, _, service = live_stack(live_server)
    conversation = await service.create(**OWNER)
    accepted = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="cancel this task"),
        scopes=SCOPES,
        idempotency_key="live-cancel",
        **OWNER,
    )
    await until_status(service, accepted.run_id, "interrupted", scopes=SCOPES)
    current = service.execution.get(accepted.run_id)["server_run_id"]
    cancelled = await service.cancel(accepted.run_id, **OWNER)
    assert cancelled.status == "cancelled"
    assert (await client.get_run(thread_id=accepted.thread_id, run_id=current))[
        "status"
    ] == "interrupted"
    next_turn = await service.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="new independent task"),
        scopes=SCOPES,
        idempotency_key="live-after-cancel",
        **OWNER,
    )
    assert next_turn.thread_id != accepted.thread_id
    assert (await service.status(accepted.run_id, **OWNER)).thread_id == accepted.thread_id
    await service.cancel(next_turn.run_id, **OWNER)
    components.database.close()
