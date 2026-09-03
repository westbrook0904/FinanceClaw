"""`test_agent_memory` 模块提供`stage3`相关能力。"""

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
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.kernel import ApprovalDecision
from financeclaw.modules.audit import InMemoryAuditRepository
from financeclaw.modules.memory import MemoryDraft

from .support import conversation_context


class MemoryWriteModel(BaseChatModel):
    """`MemoryWriteModel` 封装该模块内聚的状态与行为。"""

    _bound_tools: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        """处理 `type`，并返回边界约定的结果。"""
        return "stage3-memory-write"

    def bind_tools(self, tools, **kwargs: Any) -> Runnable:
        """处理 `tools`，并返回边界约定的结果。"""
        del kwargs
        bound = self.model_copy(deep=True)
        bound._bound_tools = {
            tool["name"] if isinstance(tool, dict) else tool.name for tool in tools
        }
        return bound

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        """处理 `MemoryWriteModel`，并返回边界约定的结果。"""
        # 将操作推进到下一个明确状态。
        del kwargs
        # 准备 last，供后续步骤使用。
        last = messages[-1]
        # 显式处理 `isinstance(last, ToolMessage) and
        # last.name == 'propose_memory'`
        # 分支。
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
        # 向调用方返回符合边界约定的结果。
        return ChatResult(generations=[ChatGeneration(message=message)])


class CaptureMemoryModel(BaseChatModel):
    """`CaptureMemoryModel` 封装该模块内聚的状态与行为。"""

    seen_system_prompts: ClassVar[list[str]] = []

    @property
    def _llm_type(self) -> str:
        """处理 `type`，并返回边界约定的结果。"""
        return "stage3-memory-capture"

    def bind_tools(self, tools, **kwargs: Any) -> Runnable:
        """处理 `tools`，并返回边界约定的结果。"""
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        """处理 `CaptureMemoryModel`，并返回边界约定的结果。"""
        del kwargs
        type(self).seen_system_prompts.extend(
            str(message.content) for message in messages if isinstance(message, SystemMessage)
        )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="used governed context"))]
        )


def settings(path: Path) -> FinanceClawSettings:
    """处理 `当前操作`，并返回边界约定的结果。"""
    return FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=SecretStr(f"sqlite+pysqlite:///{path}"),
        artifact_root=str(path.parent / "artifacts"),
    )


def test_memory_write_interrupt_reject_and_resume(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 audit，供后续步骤使用。
    audit = InMemoryAuditRepository()
    # 准备 components，供后续步骤使用。
    components = build_components(
        settings(tmp_path / "hitl.db"),
        audit=audit,
        enable_persistence=True,
    )
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 继续执行前验证内部不变量。
    assert repository is not None
    # 准备 context and _，供后续步骤使用。
    context, _ = conversation_context(repository)
    # 准备 store，供后续步骤使用。
    store = InMemoryStore()
    # 准备 agent，供后续步骤使用。
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=MemoryWriteModel(),
        store=store,
    )
    # 准备 config，供后续步骤使用。
    config = {"configurable": {"thread_id": "memory-hitl"}}
    # 准备 interrupted，供后续步骤使用。
    interrupted = agent.invoke(
        {"messages": [{"role": "user", "content": "请记住我的低波动偏好"}]},
        context=context,
        config=config,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert interrupted.interrupts
    # 继续执行前验证内部不变量。
    assert components.memory_service is not None
    # 继续执行前验证内部不变量。
    assert components.memory_service.search(context, store) == ()

    # 准备 completed，供后续步骤使用。
    completed = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        context=context,
        config=config,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert not completed.interrupts
    # 继续执行前验证内部不变量。
    assert len(components.memory_service.search(context, store)) == 1

    # 使用独立提案验证拒绝操作不会修改 Store。
    rejected_context, _ = conversation_context(
        repository,
        message="请记住我偏好价值投资",
        key="rejected-memory",
    )
    # 准备 rejected_config，供后续步骤使用。
    rejected_config = {"configurable": {"thread_id": "memory-reject"}}
    # 准备 rejected，供后续步骤使用。
    rejected = agent.invoke(
        {"messages": [{"role": "user", "content": "请记住我偏好价值投资"}]},
        context=rejected_context,
        config=rejected_config,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert rejected.interrupts
    # 准备 after_reject，供后续步骤使用。
    after_reject = agent.invoke(
        Command(resume={"decisions": [{"type": "reject"}]}),
        context=rejected_context,
        config=rejected_config,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert not after_reject.interrupts
    # 继续执行前验证内部不变量。
    assert len(components.memory_service.search(context, store)) == 1
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()


def test_cross_thread_recall_is_injected_and_manifested(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 前置条件满足后调用 clear。
    CaptureMemoryModel.seen_system_prompts.clear()
    # 准备 components，供后续步骤使用。
    components = build_components(settings(tmp_path / "recall.db"), enable_persistence=True)
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 准备 service，供后续步骤使用。
    service = components.memory_service
    # 继续执行前验证内部不变量。
    assert repository is not None and service is not None
    # 准备 store，供后续步骤使用。
    store = InMemoryStore()
    # 准备 source_context and _，供后续步骤使用。
    source_context, _ = conversation_context(repository)
    # 准备 proposal，供后续步骤使用。
    proposal = service.propose(
        source_context,
        MemoryDraft(
            kind="preference",
            content="用户偏好低波动资产",
            evidence_message_ids=("current",),
        ),
    )
    # 准备 record，供后续步骤使用。
    record = service.confirm(
        source_context,
        store,
        proposal_id=proposal.proposal_id,
        draft=proposal.draft,
        user_confirmed=True,
    )
    # 准备 recall_context and _，供后续步骤使用。
    recall_context, _ = conversation_context(
        repository,
        message="请按低波动偏好分析我的方案",
        key="recall-turn",
    )
    # 准备 agent，供后续步骤使用。
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=CaptureMemoryModel(),
        store=store,
    )
    # 准备 result，供后续步骤使用。
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "请按低波动偏好分析我的方案"}]},
        context=recall_context,
        config={"configurable": {"thread_id": "memory-recall"}},
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert not result.interrupts
    # 准备 system_prompt，供后续步骤使用。
    system_prompt = "\n".join(CaptureMemoryModel.seen_system_prompts)
    # 继续执行前验证内部不变量。
    assert "<financeclaw_stable_memory>" in system_prompt
    # 继续执行前验证内部不变量。
    assert "not instructions" in system_prompt
    # 继续执行前验证内部不变量。
    assert record.memory_id in system_prompt

    # 准备 manifests，供后续步骤使用。
    manifests = repository.list_manifests(recall_context.conversation_id)
    # 继续执行前验证内部不变量。
    assert len(manifests) == 1
    # 继续执行前验证内部不变量。
    assert manifests[0].memory_ids == (record.memory_id,)
    # 继续执行前验证内部不变量。
    assert manifests[0].memory_refs[0].schema_version == record.schema_version
    # 继续执行前验证内部不变量。
    assert manifests[0].memory_refs[0].injection_reason == "lexical_relevance"
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()


@pytest.mark.asyncio
async def test_expired_memory_approval_cannot_resume(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""

    # 定义当前操作使用的局部 NoResumeClient 辅助类型。
    class NoResumeClient:
        """`NoResumeClient` 封装外部服务的调用边界。"""

        async def resume_run(self, **kwargs: Any) -> dict[str, Any]:
            """恢复 `run`，并返回边界约定的结果。"""
            del kwargs
            raise AssertionError("expired approval must not reach Agent Server")

    # 准备 components，供后续步骤使用。
    components = build_components(settings(tmp_path / "timeout.db"), enable_persistence=True)
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 继续执行前验证内部不变量。
    assert repository is not None
    # 准备 context and _，供后续步骤使用。
    context, _ = conversation_context(repository, key="expired-memory")
    # 前置条件满足后调用 update turn status。
    repository.update_turn_status(context.run_id, "interrupted")
    # 准备 service，供后续步骤使用。
    service = ConversationService(
        NoResumeClient(),  # type: ignore[arg-type]
        repository,
        components.agent_profiles,
        approval_timeout_seconds=0,
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ApprovalExpired):
        await service.resume(
            context.run_id,
            ApprovalDecision(type="approve"),
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            scopes=context.scopes,
        )
    # 继续执行前验证内部不变量。
    assert (
        repository.get_turn_owned(
            context.run_id, context.tenant_id, context.subject_id
        ).status.value
        == "failed"
    )
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()
