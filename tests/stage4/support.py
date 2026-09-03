from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from financeclaw.application import ServerRun, WorkflowService
from financeclaw.artifacts import (
    ArtifactService,
    InMemoryArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.audit import InMemoryAuditRepository
from financeclaw.graphs.workflows import portfolio_review_definition
from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.tools import ToolCatalog, ToolPolicy, default_local_tools
from financeclaw.workflows import SqlAlchemyWorkflowRepository, WorkflowCatalog


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.value


class FakeWorkflowClient:
    """Agent Server contract double; workflow state still lives in its thread/run map."""

    def __init__(self) -> None:
        self.threads: set[str] = set()
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []

    async def create_thread(self, thread_id: str) -> None:
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
        run_id = f"server-workflow-{len(self.runs) + 1}"
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "context": context,
            "metadata": metadata,
        }
        self.create_calls.append(call)
        self.runs[run_id] = {"run_id": run_id, "status": "pending", **call}
        return ServerRun(run_id=run_id, status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        run = self.runs[run_id]
        assert run["thread_id"] == thread_id
        return run

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        run = self.runs[run_id]
        assert run["thread_id"] == thread_id
        return run["output"]

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
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
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "command": command,
            "context": context,
            "metadata": metadata,
        }
        self.resume_calls.append(call)
        decision = command["resume"]["decisions"][0]
        original = next(run for run in self.runs.values() if run["thread_id"] == thread_id)
        rejected = decision["type"] == "reject"
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
        run = next(
            run
            for run in self.runs.values()
            if run["metadata"].get("application_run_id") == application_run_id
        )
        arguments_hash = run["metadata"]["arguments_hash"]
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
        run["status"] = "interrupted"
        run["interrupts"] = [{"value": approval}]
        return approval

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        yield {"event": "values", "data": {"status": "running"}}

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        del thread_id, assistant_id
        return self._stream()

    async def health(self) -> bool:
        return True


def workflow_stack(path: Path):
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    database.initialize_schema()
    audit = InMemoryAuditRepository()
    tools = ToolCatalog(default_local_tools())
    artifacts = ArtifactService(
        SqlAlchemyArtifactRepository(database.session_factory), InMemoryArtifactStore()
    )
    definition = portfolio_review_definition(
        catalog=tools,
        policy=ToolPolicy(),
        audit=audit,
        artifact_service=artifacts,
        run_timeout_seconds=60,
        approval_timeout_seconds=60,
    )
    catalog = WorkflowCatalog((definition,))
    repository = SqlAlchemyWorkflowRepository(database.session_factory)
    fake = FakeWorkflowClient()
    clock = MutableClock()
    service = WorkflowService(fake, repository, catalog, audit, clock=clock)
    return database, repository, catalog, audit, fake, clock, service


def workflow_arguments(name: str = "Core") -> dict[str, Any]:
    return {
        "portfolio_name": name,
        "positions": [{"symbol": "AAPL", "quantity": "2", "cost_basis": "80"}],
    }
