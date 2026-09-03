import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import PrivateAttr, SecretStr

from financeclaw.application import ApprovalExpired, ConversationService
from financeclaw.audit import InMemoryAuditRepository
from financeclaw.bootstrap import build_components
from financeclaw.contracts import ApprovalDecision
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.memory import MemoryDraft

from .support import conversation_context


class MemoryWriteModel(BaseChatModel):
    """Deterministically proposes, then confirms, exactly one preference."""

    _bound_tools: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "stage3-memory-write"

    def bind_tools(self, tools, **kwargs: Any) -> Runnable:
        del kwargs
        bound = self.model_copy(deep=True)
        bound._bound_tools = {
            tool["name"] if isinstance(tool, dict) else tool.name for tool in tools
        }
        return bound

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        del kwargs
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == "propose_memory":
            proposal = json.loads(str(last.content))
            draft = proposal["draft"]
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "confirm_memory",
                        "args": {"proposal_id": proposal["proposal_id"], **draft},
                        "id": "confirm-memory-call",
                        "type": "tool_call",
                    }
                ],
            )
        elif isinstance(last, ToolMessage):
            message = AIMessage(content=f"Memory result: {last.content}")
        else:
            assert "propose_memory" in self._bound_tools
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "propose_memory",
                        "args": {
                            "kind": "preference",
                            "content": "用户偏好低波动资产",
                            "evidence_message_ids": ["current"],
                        },
                        "id": "propose-memory-call",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class CaptureMemoryModel(BaseChatModel):
    """Capture the final system prompt without selecting any tools."""

    seen_system_prompts: ClassVar[list[str]] = []

    @property
    def _llm_type(self) -> str:
        return "stage3-memory-capture"

    def bind_tools(self, tools, **kwargs: Any) -> Runnable:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        del kwargs
        type(self).seen_system_prompts.extend(
            str(message.content) for message in messages if isinstance(message, SystemMessage)
        )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="used governed context"))]
        )


def settings(path: Path) -> FinanceClawSettings:
    return FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=SecretStr(f"sqlite+pysqlite:///{path}"),
        artifact_root=str(path.parent / "artifacts"),
    )


def test_memory_write_interrupt_reject_and_resume(tmp_path: Path) -> None:
    audit = InMemoryAuditRepository()
    components = build_components(
        settings(tmp_path / "hitl.db"),
        audit=audit,
        enable_persistence=True,
    )
    repository = components.conversation_repository
    assert repository is not None
    context, _ = conversation_context(repository)
    store = InMemoryStore()
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=MemoryWriteModel(),
        store=store,
    )
    config = {"configurable": {"thread_id": "memory-hitl"}}
    interrupted = agent.invoke(
        {"messages": [{"role": "user", "content": "请记住我的低波动偏好"}]},
        context=context,
        config=config,
        version="v2",
    )
    assert interrupted.interrupts
    assert components.memory_service is not None
    assert components.memory_service.search(context, store) == ()

    completed = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        context=context,
        config=config,
        version="v2",
    )
    assert not completed.interrupts
    assert len(components.memory_service.search(context, store)) == 1

    # A distinct proposal demonstrates that rejection leaves Store untouched.
    rejected_context, _ = conversation_context(
        repository,
        message="请记住我偏好价值投资",
        key="rejected-memory",
    )
    rejected_config = {"configurable": {"thread_id": "memory-reject"}}
    rejected = agent.invoke(
        {"messages": [{"role": "user", "content": "请记住我偏好价值投资"}]},
        context=rejected_context,
        config=rejected_config,
        version="v2",
    )
    assert rejected.interrupts
    after_reject = agent.invoke(
        Command(resume={"decisions": [{"type": "reject"}]}),
        context=rejected_context,
        config=rejected_config,
        version="v2",
    )
    assert not after_reject.interrupts
    assert len(components.memory_service.search(context, store)) == 1
    if components.database is not None:
        components.database.close()


def test_cross_thread_recall_is_injected_and_manifested(tmp_path: Path) -> None:
    CaptureMemoryModel.seen_system_prompts.clear()
    components = build_components(settings(tmp_path / "recall.db"), enable_persistence=True)
    repository = components.conversation_repository
    service = components.memory_service
    assert repository is not None and service is not None
    store = InMemoryStore()
    source_context, _ = conversation_context(repository)
    proposal = service.propose(
        source_context,
        MemoryDraft(
            kind="preference",
            content="用户偏好低波动资产",
            evidence_message_ids=("current",),
        ),
    )
    record = service.confirm(
        source_context,
        store,
        proposal_id=proposal.proposal_id,
        draft=proposal.draft,
        user_confirmed=True,
    )
    recall_context, _ = conversation_context(
        repository,
        message="请按低波动偏好分析我的方案",
        key="recall-turn",
    )
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=CaptureMemoryModel(),
        store=store,
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "请按低波动偏好分析我的方案"}]},
        context=recall_context,
        config={"configurable": {"thread_id": "memory-recall"}},
        version="v2",
    )
    assert not result.interrupts
    system_prompt = "\n".join(CaptureMemoryModel.seen_system_prompts)
    assert "<financeclaw_stable_memory>" in system_prompt
    assert "not instructions" in system_prompt
    assert record.memory_id in system_prompt

    manifests = repository.list_manifests(recall_context.conversation_id)
    assert len(manifests) == 1
    assert manifests[0].memory_ids == (record.memory_id,)
    assert manifests[0].memory_refs[0].schema_version == record.schema_version
    assert manifests[0].memory_refs[0].injection_reason == "lexical_relevance"
    if components.database is not None:
        components.database.close()


@pytest.mark.asyncio
async def test_expired_memory_approval_cannot_resume(tmp_path: Path) -> None:
    class NoResumeClient:
        async def resume_run(self, **kwargs: Any) -> dict[str, Any]:
            del kwargs
            raise AssertionError("expired approval must not reach Agent Server")

    components = build_components(settings(tmp_path / "timeout.db"), enable_persistence=True)
    repository = components.conversation_repository
    assert repository is not None
    context, _ = conversation_context(repository, key="expired-memory")
    repository.update_turn_status(context.run_id, "interrupted")
    service = ConversationService(
        NoResumeClient(),  # type: ignore[arg-type]
        repository,
        components.agent_profiles,
        approval_timeout_seconds=0,
    )
    with pytest.raises(ApprovalExpired):
        await service.resume(
            context.run_id,
            ApprovalDecision(type="approve"),
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            scopes=context.scopes,
        )
    assert (
        repository.get_turn_owned(
            context.run_id, context.tenant_id, context.subject_id
        ).status.value
        == "failed"
    )
    if components.database is not None:
        components.database.close()
