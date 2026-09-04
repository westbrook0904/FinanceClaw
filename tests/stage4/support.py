"""`support` 模块提供`stage4`相关能力。"""

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from financeclaw.application import ServerRun, WorkflowService
from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.modules.artifacts import (
    ArtifactService,
    InMemoryArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.modules.audit import InMemoryAuditRepository
from financeclaw.modules.workflows import SqlAlchemyWorkflowRepository, WorkflowCatalog
from financeclaw.orchestration.graphs.workflows import portfolio_review_definition
from financeclaw.orchestration.tools import ToolCatalog, ToolPolicy, default_local_tools


class MutableClock:
    """`MutableClock` 封装该模块内聚的状态与行为。"""

    def __init__(self) -> None:
        """初始化 `MutableClock` 及其必需的协作对象。"""
        self.value = datetime.now(UTC)

    def __call__(self) -> datetime:
        """执行当前实例定义的可调用行为。"""
        return self.value


class FakeWorkflowClient:
    """`FakeWorkflowClient` 封装外部服务的调用边界。"""

    def __init__(self) -> None:
        """初始化 `FakeWorkflowClient` 及其必需的协作对象。"""
        self.threads: set[str] = set()
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []

    async def create_thread(self, thread_id: str) -> None:
        """创建 `thread`，并返回边界约定的结果。"""
        self.threads.add(thread_id)

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        """创建 `run`，并返回边界约定的结果。"""
        # 准备 run_id，供后续步骤使用。
        run_id = f"server-workflow-{len(self.runs) + 1}"
        # 准备 call，供后续步骤使用。
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "context": context,
            "metadata": metadata,
        }
        # 前置条件满足后调用 append。
        self.create_calls.append(call)
        # 准备 working state，供后续步骤使用。
        self.runs[run_id] = {"run_id": run_id, "status": "pending", **call}
        # 向调用方返回符合边界约定的结果。
        return ServerRun(run_id=run_id, status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """获取 `run`，并返回边界约定的结果。"""
        run = self.runs[run_id]
        assert run["thread_id"] == thread_id
        return run

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """等待并合并 `run`，并返回边界约定的结果。"""
        run = self.runs[run_id]
        assert run["thread_id"] == thread_id
        return run["output"]

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """查找 `run`，并返回边界约定的结果。"""
        for run_id, run in self.runs.items():
            if (
                run["thread_id"] == thread_id
                and run["metadata"].get("application_run_id") == application_run_id
            ):
                return ServerRun(run_id=run_id, status=str(run["status"]))
        return None

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        """恢复 `run`，并返回边界约定的结果。"""
        # 准备 call，供后续步骤使用。
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "command": command,
            "context": context,
            "metadata": metadata,
        }
        # 前置条件满足后调用 append。
        self.resume_calls.append(call)
        # 准备 decision，供后续步骤使用。
        decision = command["resume"]["decisions"][0]
        # 准备 original，供后续步骤使用。
        original = next(run for run in self.runs.values() if run["thread_id"] == thread_id)
        # 准备 rejected，供后续步骤使用。
        rejected = decision["type"] == "reject"
        # 向调用方返回符合边界约定的结果。
        return {
            "workflow_id": "portfolio_review",
            "workflow_version": "1.0.0",
            "run_id": metadata["application_run_id"],
            "status": "rejected" if rejected else "completed",
            "arguments_hash": metadata["arguments_hash"],
            "portfolio_name": original["input"]["portfolio_name"],
            "source_refs": [],
            "artifact": (
                None
                if rejected
                else {
                    "artifact_id": "artifact-test-report",
                    "content_type": "application/json",
                    "content_hash": "0" * 64,
                    "size_bytes": 42,
                }
            ),
            "error": "report publication rejected" if rejected else None,
        }

    def interrupt(self, application_run_id: str) -> dict[str, Any]:
        """处理 `FakeWorkflowClient`，并返回边界约定的结果。"""
        # 准备 run，供后续步骤使用。
        run = next(
            run
            for run in self.runs.values()
            if run["metadata"].get("application_run_id") == application_run_id
        )
        # 准备 arguments_hash，供后续步骤使用。
        arguments_hash = run["metadata"]["arguments_hash"]
        # 准备 approval，供后续步骤使用。
        approval = {
            "approval_id": f"approval-{application_run_id}",
            "approval_point": "publish_portfolio_report",
            "workflow_id": "portfolio_review",
            "workflow_version": "1.0.0",
            "requested_action": "publish_portfolio_report",
            "arguments_hash": arguments_hash,
            "allowed_decisions": ["approve", "reject"],
            "required_scope": "workflows:approve",
            "summary": {"risk_band": "high"},
        }
        # 准备 working state，供后续步骤使用。
        run["status"] = "interrupted"
        # 准备 working state，供后续步骤使用。
        run["interrupts"] = [{"value": approval}]
        # 向调用方返回符合边界约定的结果。
        return approval

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        """以流式方式输出 `FakeWorkflowClient`，并返回边界约定的结果。"""
        yield {"event": "values", "data": {"status": "running"}}

    def stream_run(self, *, thread_id: str, run_id: str) -> AsyncIterator[Any]:
        """以流式方式输出指定 `run`，并返回边界约定的结果。"""
        del thread_id, run_id
        return self._stream()

    async def health(self) -> bool:
        """检查健康状态 `FakeWorkflowClient`，并返回边界约定的结果。"""
        return True


def workflow_stack(path: Path):
    """处理 `stack`，并返回边界约定的结果。"""
    # 准备 database，供后续步骤使用。
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    # 前置条件满足后调用 initialize schema。
    database.initialize_schema()
    # 准备 audit，供后续步骤使用。
    audit = InMemoryAuditRepository()
    # 准备 tools，供后续步骤使用。
    tools = ToolCatalog(default_local_tools())
    # 准备 artifacts，供后续步骤使用。
    artifacts = ArtifactService(
        SqlAlchemyArtifactRepository(database.session_factory), InMemoryArtifactStore()
    )
    # 准备 definition，供后续步骤使用。
    definition = portfolio_review_definition(
        catalog=tools,
        policy=ToolPolicy(),
        audit=audit,
        artifact_service=artifacts,
        run_timeout_seconds=60,
        approval_timeout_seconds=60,
    )
    # 准备 catalog，供后续步骤使用。
    catalog = WorkflowCatalog((definition,))
    # 准备 repository，供后续步骤使用。
    repository = SqlAlchemyWorkflowRepository(database.session_factory)
    # 准备 fake，供后续步骤使用。
    fake = FakeWorkflowClient()
    # 准备 clock，供后续步骤使用。
    clock = MutableClock()
    # 准备 service，供后续步骤使用。
    service = WorkflowService(fake, repository, catalog, audit, clock=clock)
    # 向调用方返回符合边界约定的结果。
    return database, repository, catalog, audit, fake, clock, service


def workflow_arguments(name: str = "Core") -> dict[str, Any]:
    """处理 `arguments`，并返回边界约定的结果。"""
    return {
        "portfolio_name": name,
        "positions": [{"symbol": "AAPL", "quantity": "2", "cost_basis": "80"}],
    }
