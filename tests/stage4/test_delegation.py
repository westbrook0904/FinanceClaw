from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command
from pydantic import SecretStr

from financeclaw.agents import OfflineFinanceModel
from financeclaw.application import (
    ConversationService,
    DelegationService,
    ServerRun,
    WorkflowService,
)
from financeclaw.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.bootstrap import build_components
from financeclaw.contracts import ApprovalDecision, ConversationTurnRequest, ExecutionContext
from financeclaw.delegation import (
    AgentHandoff,
    DelegationResult,
    DelegationStatus,
    SqlAlchemyDelegationRepository,
    WorkflowHandoff,
)
from financeclaw.infrastructure import FinanceClawSettings

from .support import workflow_arguments

SCOPES = frozenset({"market:read", "portfolio:review", "workflows:approve"})


class FakeDelegationClient:
    """Agent Server double with separate parent and domain-Agent run state."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []

    async def create_thread(self, thread_id: str) -> None:
        del thread_id

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        server_run_id = f"server-{len(self.runs) + 1}"
        call = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "context": context,
            "metadata": metadata,
        }
        self.create_calls.append(call)
        if assistant_id == "finance_agent":
            handoff = AgentHandoff(
                handoff_id=f"delegation-{context['run_id']}",
                parent_run_id=context["run_id"],
                parent_turn_id=context["turn_id"],
                conversation_id=context["conversation_id"],
                agent_id="market_research_agent",
                task="Research AAPL with current market evidence",
            )
            state: dict[str, Any] = {
                "status": "interrupted",
                "interrupts": [{"value": handoff.model_dump(mode="json")}],
            }
        else:
            state = {
                "status": "pending",
                "output": {"messages": [AIMessage(content="AAPL evidence summary")]},
            }
        self.runs[server_run_id] = {"run_id": server_run_id, **call, **state}
        return ServerRun(run_id=server_run_id, status=str(state["status"]))

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]["output"]

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
        if "decisions" in command["resume"]:
            workflow = next(run for run in self.runs.values() if run["thread_id"] == thread_id)
            return {
                "workflow_id": "portfolio_review",
                "workflow_version": "1.0.0",
                "run_id": metadata["application_run_id"],
                "status": "completed",
                "arguments_hash": metadata["arguments_hash"],
                "portfolio_name": workflow["input"]["portfolio_name"],
                "source_refs": [],
                "artifact": {
                    "artifact_id": "artifact-delegated-workflow",
                    "content_type": "application/json",
                    "content_hash": "0" * 64,
                    "size_bytes": 42,
                },
                "error": None,
            }
        return {"messages": [AIMessage(content="Parent synthesized the child result")]}

    def interrupt_workflow(self, application_run_id: str) -> dict[str, Any]:
        run = next(
            run
            for run in self.runs.values()
            if run["metadata"].get("application_run_id") == application_run_id
        )
        approval = {
            "approval_id": f"approval-{application_run_id}",
            "approval_point": "publish_portfolio_report",
            "workflow_id": "portfolio_review",
            "workflow_version": "1.0.0",
            "requested_action": "publish_portfolio_report",
            "arguments_hash": run["metadata"]["arguments_hash"],
            "allowed_decisions": ["approve", "reject"],
            "required_scope": "workflows:approve",
            "summary": {"risk_band": "moderate"},
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


def _components(path: Path):
    return build_components(
        FinanceClawSettings(
            environment="test",
            offline_model=True,
            database_url=SecretStr(f"sqlite+pysqlite:///{path}"),
            artifact_root=str(path.parent / "artifacts"),
        ),
        enable_persistence=True,
    )


def test_domain_agent_directive_emits_and_consumes_a_typed_handoff() -> None:
    components = build_components(FinanceClawSettings(environment="test", offline_model=True))
    graph = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    context = ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        conversation_id="conversation-a",
        turn_id="turn-a",
        run_id="parent-run-a",
    )
    config = {"configurable": {"thread_id": "typed-handoff"}}

    interrupted = graph.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "/agent market_research_agent research AAPL",
                }
            ]
        },
        config=config,
        context=context,
        version="v2",
    )
    handoff = AgentHandoff.model_validate(interrupted.interrupts[0].value)
    assert handoff.parent_run_id == context.run_id
    assert handoff.task == "research AAPL"

    resumed = graph.invoke(
        Command(
            resume=DelegationResult(
                delegation_id=handoff.handoff_id,
                kind="agent",
                target_id=handoff.agent_id,
                target_version="1.0.0",
                child_run_id="child-run-a",
                status="completed",
                output={"message": "bounded child result"},
            ).model_dump(mode="json")
        ),
        config=config,
        context=context,
        version="v2",
    )
    assert "child-run-a" in resumed.value["messages"][-1].content


def test_explicit_agent_directive_cannot_bypass_scope_visibility() -> None:
    components = build_components(FinanceClawSettings(environment="test", offline_model=True))
    graph = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    context = ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(),
        conversation_id="conversation-a",
        turn_id="turn-denied",
        run_id="parent-run-denied",
    )

    result = graph.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "/agent market_research_agent research AAPL",
                }
            ]
        },
        config={"configurable": {"thread_id": "denied-handoff"}},
        context=context,
        version="v2",
    )

    assert not result.interrupts
    assert "no registered delegation capability" in result.value["messages"][-1].content


@pytest.mark.asyncio
async def test_parent_child_mapping_survives_restart_and_resumes_parent(tmp_path: Path) -> None:
    database_path = tmp_path / "delegation-restart.db"
    components = _components(database_path)
    assert components.database is not None
    assert components.conversation_repository is not None
    assert components.workflow_repository is not None
    assert components.workflow_catalog is not None
    assert components.delegation_repository is not None
    fake = FakeDelegationClient()
    audit = InMemoryAuditRepository()
    workflow_service = WorkflowService(
        fake,
        components.workflow_repository,
        components.workflow_catalog,
        audit,
    )
    delegation_service = DelegationService(
        fake,
        components.delegation_repository,
        workflow_service,
        components.agent_profiles,
        audit,
    )
    conversations = ConversationService(
        fake,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=delegation_service,
    )
    conversation = await conversations.create(tenant_id="tenant-a", subject_id="subject-a")
    accepted = await conversations.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="research AAPL"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="delegate-domain-agent",
    )
    waiting = await conversations.status(
        accepted.run_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )
    assert waiting.status == "waiting_child"
    child_run_id = waiting.output["delegation"]["child_run_id"]
    child_call = fake.create_calls[-1]
    assert child_run_id != accepted.run_id
    assert child_call["thread_id"] != accepted.thread_id
    assert child_call["metadata"]["parent_run_id"] == accepted.run_id

    # Recompose repositories and services to model a BFF process restart.
    restarted_repository = SqlAlchemyDelegationRepository(components.database.session_factory)
    restarted_workflows = WorkflowService(
        fake,
        components.workflow_repository,
        components.workflow_catalog,
        audit,
    )
    restarted_delegations = DelegationService(
        fake,
        restarted_repository,
        restarted_workflows,
        components.agent_profiles,
        audit,
    )
    restarted_conversations = ConversationService(
        fake,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=restarted_delegations,
    )
    child_server_run_id = child_call["metadata"]["application_run_id"]
    child_server = next(
        run
        for run in fake.runs.values()
        if run["metadata"].get("application_run_id") == child_server_run_id
    )
    child_server["status"] = "success"

    completed = await restarted_conversations.status(
        accepted.run_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )
    assert completed.status == "completed"
    assert fake.resume_calls[-1]["command"]["resume"]["child_run_id"] == child_run_id
    persisted = restarted_repository.get_by_child_owned(
        child_run_id,
        "tenant-a",
        "subject-a",
    )
    assert persisted.status is DelegationStatus.DELIVERED
    assert [event.event_type for event in audit.records()] == [
        AuditEventType.DELEGATION_REQUESTED,
        AuditEventType.DELEGATION_STARTED,
        AuditEventType.DELEGATION_COMPLETED,
        AuditEventType.DELEGATION_DELIVERED,
    ]
    components.database.close()


@pytest.mark.asyncio
async def test_workflow_handoff_is_revalidated_and_bound_to_an_independent_run(
    tmp_path: Path,
) -> None:
    components = _components(tmp_path / "workflow-delegation.db")
    assert components.database is not None
    assert components.conversation_repository is not None
    assert components.workflow_repository is not None
    assert components.workflow_catalog is not None
    assert components.delegation_repository is not None
    fake = FakeDelegationClient()
    audit = InMemoryAuditRepository()
    workflow_service = WorkflowService(
        fake,
        components.workflow_repository,
        components.workflow_catalog,
        audit,
    )
    service = DelegationService(
        fake,
        components.delegation_repository,
        workflow_service,
        components.agent_profiles,
        audit,
    )
    conversation = components.conversation_repository.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    turn, _, _ = components.conversation_repository.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="workflow-parent",
        request_hash="0" * 64,
        message="review my portfolio",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    handoff = WorkflowHandoff(
        handoff_id="delegation-workflow-a",
        parent_run_id=turn.run_id,
        parent_turn_id=turn.turn_id,
        conversation_id=conversation.conversation_id,
        workflow_id="portfolio_review",
        arguments=workflow_arguments(),
    )

    record = await service.start(
        handoff,
        parent_run_id=turn.run_id,
        parent_turn_id=turn.turn_id,
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )

    assert record.kind.value == "workflow"
    assert record.target_version == "1.0.0"
    assert record.child_run_id != turn.run_id
    assert record.child_thread_id != conversation.agent_thread_id
    assert fake.create_calls[-1]["assistant_id"] == "portfolio_review_v1"
    approval = fake.interrupt_workflow(record.child_run_id)
    interrupted = await service.status(
        record.delegation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
    )
    assert interrupted.status is DelegationStatus.INTERRUPTED
    completed = await service.resume(
        interrupted,
        ApprovalDecision(
            type="approve",
            arguments_hash=approval["arguments_hash"],
        ),
        scopes=SCOPES,
    )
    assert completed.status is DelegationStatus.COMPLETED
    assert completed.output_payload["artifact"]["artifact_id"] == ("artifact-delegated-workflow")
    components.database.close()
