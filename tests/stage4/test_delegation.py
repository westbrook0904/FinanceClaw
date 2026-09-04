"""`test_delegation` 模块提供`stage4`相关能力。"""

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command
from pydantic import SecretStr

from financeclaw.application import (
    ConversationService,
    DelegationService,
    ServerRun,
    WorkflowService,
)
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.kernel import ApprovalDecision, ConversationTurnRequest, ExecutionContext
from financeclaw.modules.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.modules.delegation import (
    AgentHandoff,
    DelegationResult,
    DelegationStatus,
    SqlAlchemyDelegationRepository,
    WorkflowHandoff,
)
from financeclaw.orchestration.agents import OfflineFinanceModel

from .support import workflow_arguments

SCOPES = frozenset({"market:read", "portfolio:review", "workflows:approve"})


class FakeDelegationClient:
    """`FakeDelegationClient` 封装外部服务的调用边界。"""

    def __init__(self) -> None:
        """初始化 `FakeDelegationClient` 及其必需的协作对象。"""
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []

    async def create_thread(self, thread_id: str) -> None:
        """创建 `thread`，并返回边界约定的结果。"""
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
        """创建 `run`，并返回边界约定的结果。"""
        # 准备 server_run_id，供后续步骤使用。
        server_run_id = f"server-{len(self.runs) + 1}"
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
        # 显式处理 `assistant_id == 'finance_agent'` 分支。
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
        # 准备 working state，供后续步骤使用。
        self.runs[server_run_id] = {"run_id": server_run_id, **call, **state}
        # 向调用方返回符合边界约定的结果。
        return ServerRun(run_id=server_run_id, status=str(state["status"]))

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """获取 `run`，并返回边界约定的结果。"""
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """等待并合并 `run`，并返回边界约定的结果。"""
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]["output"]

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
        # 显式处理 `'decisions' in command['resume']` 分支。
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
        # 向调用方返回符合边界约定的结果。
        return {"messages": [AIMessage(content="Parent synthesized the child result")]}

    def interrupt_workflow(self, application_run_id: str) -> dict[str, Any]:
        """处理 `workflow`，并返回边界约定的结果。"""
        # 准备 run，供后续步骤使用。
        run = next(
            run
            for run in self.runs.values()
            if run["metadata"].get("application_run_id") == application_run_id
        )
        # 准备 approval，供后续步骤使用。
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
        # 准备 working state，供后续步骤使用。
        run["status"] = "interrupted"
        # 准备 working state，供后续步骤使用。
        run["interrupts"] = [{"value": approval}]
        # 向调用方返回符合边界约定的结果。
        return approval

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        """以流式方式输出 `FakeDelegationClient`，并返回边界约定的结果。"""
        yield {"event": "values", "data": {"status": "running"}}

    def stream_run(self, *, thread_id: str, run_id: str) -> AsyncIterator[Any]:
        """以流式方式输出指定 `run`，并返回边界约定的结果。"""
        del thread_id, run_id
        return self._stream()

    async def health(self) -> bool:
        """检查健康状态 `FakeDelegationClient`，并返回边界约定的结果。"""
        return True


def _components(path: Path):
    """处理 `当前操作`，并返回边界约定的结果。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = build_components(FinanceClawSettings(environment="test", offline_model=True))
    # 准备 graph，供后续步骤使用。
    graph = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    # 准备 context，供后续步骤使用。
    context = ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset({"market:read"}),
        conversation_id="conversation-a",
        turn_id="turn-a",
        run_id="parent-run-a",
    )
    # 准备 config，供后续步骤使用。
    config = {"configurable": {"thread_id": "typed-handoff"}}

    # 准备 interrupted，供后续步骤使用。
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
    # 准备 handoff，供后续步骤使用。
    handoff = AgentHandoff.model_validate(interrupted.interrupts[0].value)
    # 继续执行前验证内部不变量。
    assert handoff.parent_run_id == context.run_id
    # 继续执行前验证内部不变量。
    assert handoff.task == "research AAPL"

    # 准备 resumed，供后续步骤使用。
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
    # 继续执行前验证内部不变量。
    assert "child-run-a" in resumed.value["messages"][-1].content


def test_explicit_agent_directive_cannot_bypass_scope_visibility() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = build_components(FinanceClawSettings(environment="test", offline_model=True))
    # 准备 graph，供后续步骤使用。
    graph = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    # 准备 context，供后续步骤使用。
    context = ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(),
        conversation_id="conversation-a",
        turn_id="turn-denied",
        run_id="parent-run-denied",
    )

    # 准备 result，供后续步骤使用。
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

    # 继续执行前验证内部不变量。
    assert not result.interrupts
    # 继续执行前验证内部不变量。
    assert "no registered delegation capability" in result.value["messages"][-1].content


@pytest.mark.asyncio
async def test_parent_child_mapping_survives_restart_and_resumes_parent(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database_path，供后续步骤使用。
    database_path = tmp_path / "delegation-restart.db"
    # 准备 components，供后续步骤使用。
    components = _components(database_path)
    # 继续执行前验证内部不变量。
    assert components.database is not None
    # 继续执行前验证内部不变量。
    assert components.conversation_repository is not None
    # 继续执行前验证内部不变量。
    assert components.workflow_repository is not None
    # 继续执行前验证内部不变量。
    assert components.workflow_catalog is not None
    # 继续执行前验证内部不变量。
    assert components.delegation_repository is not None
    # 准备 fake，供后续步骤使用。
    fake = FakeDelegationClient()
    # 准备 audit，供后续步骤使用。
    audit = InMemoryAuditRepository()
    # 准备 workflow_service，供后续步骤使用。
    workflow_service = WorkflowService(
        fake,
        components.workflow_repository,
        components.workflow_catalog,
        audit,
    )
    # 准备 delegation_service，供后续步骤使用。
    delegation_service = DelegationService(
        fake,
        components.delegation_repository,
        workflow_service,
        components.agent_profiles,
        audit,
    )
    # 准备 conversations，供后续步骤使用。
    conversations = ConversationService(
        fake,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=delegation_service,
    )
    # 准备 conversation，供后续步骤使用。
    conversation = await conversations.create(tenant_id="tenant-a", subject_id="subject-a")
    # 准备 accepted，供后续步骤使用。
    accepted = await conversations.start_turn(
        conversation.conversation_id,
        ConversationTurnRequest(message="research AAPL"),
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
        idempotency_key="delegate-domain-agent",
    )
    # 准备 waiting，供后续步骤使用。
    waiting = await conversations.status(
        accepted.run_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )
    # 继续执行前验证内部不变量。
    assert waiting.status == "waiting_child"
    # 准备 child_run_id，供后续步骤使用。
    child_run_id = waiting.output["delegation"]["child_run_id"]
    # 准备 child_call，供后续步骤使用。
    child_call = fake.create_calls[-1]
    # 继续执行前验证内部不变量。
    assert child_run_id != accepted.run_id
    # 继续执行前验证内部不变量。
    assert child_call["thread_id"] != accepted.thread_id
    # 继续执行前验证内部不变量。
    assert child_call["metadata"]["parent_run_id"] == accepted.run_id

    # 重新组装 Repository 和 Service，以模拟 BFF 进程重启。
    restarted_repository = SqlAlchemyDelegationRepository(components.database.session_factory)
    # 准备 restarted_workflows，供后续步骤使用。
    restarted_workflows = WorkflowService(
        fake,
        components.workflow_repository,
        components.workflow_catalog,
        audit,
    )
    # 准备 restarted_delegations，供后续步骤使用。
    restarted_delegations = DelegationService(
        fake,
        restarted_repository,
        restarted_workflows,
        components.agent_profiles,
        audit,
    )
    # 准备 restarted_conversations，供后续步骤使用。
    restarted_conversations = ConversationService(
        fake,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=restarted_delegations,
    )
    # 准备 child_server_run_id，供后续步骤使用。
    child_server_run_id = child_call["metadata"]["application_run_id"]
    # 准备 child_server，供后续步骤使用。
    child_server = next(
        run
        for run in fake.runs.values()
        if run["metadata"].get("application_run_id") == child_server_run_id
    )
    # 准备 working state，供后续步骤使用。
    child_server["status"] = "success"

    # 准备 completed，供后续步骤使用。
    completed = await restarted_conversations.status(
        accepted.run_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )
    # 继续执行前验证内部不变量。
    assert completed.status == "completed"
    # 继续执行前验证内部不变量。
    assert fake.resume_calls[-1]["command"]["resume"]["child_run_id"] == child_run_id
    # 准备 persisted，供后续步骤使用。
    persisted = restarted_repository.get_by_child_owned(
        child_run_id,
        "tenant-a",
        "subject-a",
    )
    # 继续执行前验证内部不变量。
    assert persisted.status is DelegationStatus.DELIVERED
    # 继续执行前验证内部不变量。
    assert [event.event_type for event in audit.records()] == [
        AuditEventType.DELEGATION_REQUESTED,
        AuditEventType.DELEGATION_STARTED,
        AuditEventType.DELEGATION_COMPLETED,
        AuditEventType.DELEGATION_DELIVERED,
    ]
    # 前置条件满足后调用 close。
    components.database.close()


@pytest.mark.asyncio
async def test_workflow_handoff_is_revalidated_and_bound_to_an_independent_run(
    tmp_path: Path,
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = _components(tmp_path / "workflow-delegation.db")
    # 继续执行前验证内部不变量。
    assert components.database is not None
    # 继续执行前验证内部不变量。
    assert components.conversation_repository is not None
    # 继续执行前验证内部不变量。
    assert components.workflow_repository is not None
    # 继续执行前验证内部不变量。
    assert components.workflow_catalog is not None
    # 继续执行前验证内部不变量。
    assert components.delegation_repository is not None
    # 准备 fake，供后续步骤使用。
    fake = FakeDelegationClient()
    # 准备 audit，供后续步骤使用。
    audit = InMemoryAuditRepository()
    # 准备 workflow_service，供后续步骤使用。
    workflow_service = WorkflowService(
        fake,
        components.workflow_repository,
        components.workflow_catalog,
        audit,
    )
    # 准备 service，供后续步骤使用。
    service = DelegationService(
        fake,
        components.delegation_repository,
        workflow_service,
        components.agent_profiles,
        audit,
    )
    # 准备 conversation，供后续步骤使用。
    conversation = components.conversation_repository.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    # 准备 turn and _ and _，供后续步骤使用。
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
    # 准备 handoff，供后续步骤使用。
    handoff = WorkflowHandoff(
        handoff_id="delegation-workflow-a",
        parent_run_id=turn.run_id,
        parent_turn_id=turn.turn_id,
        conversation_id=conversation.conversation_id,
        workflow_id="portfolio_review",
        arguments=workflow_arguments(),
    )

    # 准备 record，供后续步骤使用。
    record = await service.start(
        handoff,
        parent_run_id=turn.run_id,
        parent_turn_id=turn.turn_id,
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=SCOPES,
    )

    # 继续执行前验证内部不变量。
    assert record.kind.value == "workflow"
    # 继续执行前验证内部不变量。
    assert record.target_version == "1.0.0"
    # 继续执行前验证内部不变量。
    assert record.child_run_id != turn.run_id
    # 继续执行前验证内部不变量。
    assert record.child_thread_id != conversation.agent_thread_id
    # 继续执行前验证内部不变量。
    assert fake.create_calls[-1]["assistant_id"] == "portfolio_review_v1"
    # 准备 approval，供后续步骤使用。
    approval = fake.interrupt_workflow(record.child_run_id)
    # 准备 interrupted，供后续步骤使用。
    interrupted = await service.status(
        record.delegation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
    )
    # 继续执行前验证内部不变量。
    assert interrupted.status is DelegationStatus.INTERRUPTED
    # 准备 completed，供后续步骤使用。
    completed = await service.resume(
        interrupted,
        ApprovalDecision(
            type="approve",
            arguments_hash=approval["arguments_hash"],
        ),
        scopes=SCOPES,
    )
    # 继续执行前验证内部不变量。
    assert completed.status is DelegationStatus.COMPLETED
    # 继续执行前验证内部不变量。
    assert completed.output_payload["artifact"]["artifact_id"] == ("artifact-delegated-workflow")
    # 前置条件满足后调用 close。
    components.database.close()
