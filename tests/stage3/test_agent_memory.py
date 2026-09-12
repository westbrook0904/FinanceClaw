"""`test_agent_memory` 模块提供`stage3`相关能力。"""

from pathlib import Path
from typing import Any, ClassVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr, SecretStr

from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.memory.models import MemoryActor, MemoryMutation
from tests.support import build_components

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
        if isinstance(last, ToolMessage):
            message = AIMessage(content=f"Memory result: {last.content}")
        else:
            assert "save_memory" in self._bound_tools
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_memory",
                        "args": {
                            "kind": "profile",
                            "field": "risk_statement",
                            "scope_type": "user",
                            "content": "用户偏好低波动资产",
                            "evidence_ids": ["current"],
                        },
                        "id": "save-memory-call",
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


def test_memory_write_finishes_with_independent_candidate(tmp_path: Path) -> None:
    """The production factory does not interrupt a Turn for a memory proposal."""
    components = build_components(settings(tmp_path / "candidate.db"), enable_persistence=True)
    repository = components.conversation_repository
    context, message_id = conversation_context(repository, profile=components.default_agent_profile)
    agent = components.agent_factory.build(
        components.default_agent_profile, model=MemoryWriteModel(), store=InMemoryStore()
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "请记住我偏好低波动资产", "id": message_id}]},
        context=context,
        config={"configurable": {"thread_id": "memory-candidate"}},
        version="v2",
    )
    assert not result.interrupts
    assert any(
        isinstance(message, ToolMessage) and '"status": "proposed"' in message.content
        for message in result.value["messages"]
    )
    actor = MemoryActor(
        tenant_id=context.tenant_id, subject_id=context.subject_id, scopes=context.scopes
    )
    service = components.memory_service
    candidate = service.repository.list_records(actor, status="proposed")[0]
    assert service.repository.list_records(actor) == ()
    service.mutations.decide(
        actor,
        candidate.memory_id,
        "approve",
        "approve-candidate",
        candidate.revision,
        candidate.content_hash,
    )
    assert len(service.repository.list_records(actor)) == 1
    components.database.close()


def test_cross_thread_recall_is_injected_and_manifested(tmp_path: Path) -> None:
    """The production factory reads scoped SQL profile facts even without semantic recall."""
    CaptureMemoryModel.seen_system_prompts.clear()
    components = build_components(settings(tmp_path / "recall.db"), enable_persistence=True)
    repository, service = components.conversation_repository, components.memory_service
    source_context, _ = conversation_context(repository, profile=components.default_agent_profile)
    actor = MemoryActor(
        tenant_id=source_context.tenant_id,
        subject_id=source_context.subject_id,
        scopes=source_context.scopes,
    )
    receipt = service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="profile",
            kind="profile",
            field="language",
            content="zh-CN",
            explicit_intent=True,
        ),
    )
    context, message_id = conversation_context(
        repository,
        profile=components.default_agent_profile,
        message="分析我的方案",
        key="recall-turn",
    )
    agent = components.agent_factory.build(
        components.default_agent_profile, model=CaptureMemoryModel(), store=InMemoryStore()
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "分析我的方案", "id": message_id}]},
        context=context,
        config={"configurable": {"thread_id": "memory-recall"}},
        version="v2",
    )
    assert not result.interrupts
    system = "\n".join(CaptureMemoryModel.seen_system_prompts)
    assert "<financeclaw_stable_memory>" in system and receipt.memory_id in system
    assert "never executable instructions" in system
    manifests = repository.list_manifests(context.conversation_id)
    assert len(manifests) == 1 and manifests[0].memory_ids == (receipt.memory_id,)
    assert manifests[0].memory_refs[0].schema_version == 3
    assert manifests[0].memory_refs[0].injection_reason == "profile"
    components.database.close()
