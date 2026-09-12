"""Stage 11 原生 Agent/checkpoint/reducer 探针，覆盖恢复与完整工具批次。"""

from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver, PersistentDict
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from financeclaw.agent_server.context.compaction import NativeContextMiddleware
from financeclaw.agent_server.context.planning import completed_tool_batches
from financeclaw.agent_server.context.state import ConversationState, WorkingContextDraft
from financeclaw.agent_server.middleware.final_context import (
    FinalContextMiddleware,
    RequestRecorder,
)
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.turns import current_turn_start
from financeclaw.shared.llm.budget import ContextBudget


@pytest.mark.asyncio
async def test_native_graph_factory_keeps_cold_initialization_off_the_event_loop(monkeypatch):
    """真实HTTP探针发现的冷启动阻塞永久回归：编译与tokenizer探测必须离开ASGI线程。"""
    import threading

    from financeclaw.agent_server.graphs import product

    loop_thread = threading.get_ident()
    graph = object()

    def cold_graph():
        """模拟需要同步I/O的第一次图编译，拒绝在ASGI事件循环执行。"""
        assert threading.get_ident() != loop_thread
        return graph

    monkeypatch.setattr(product, "_graph", cold_graph)
    assert await product.finance_agent({}) is graph


class BoundFakeModel(FakeMessagesListChatModel):
    """保留完整原生 Agent 执行路径，仅替换远端模型传输。"""

    def bind_tools(self, tools, **kwargs):
        """测试模型接受框架工具绑定，由预设响应声明真实调用。"""
        return self


class Archive:
    """记录所有先于摘要提交归档的工具原文。"""

    def __init__(self):
        """为每个测试隔离已归档结果。"""
        self.saved = []

    def save(self, message, context):
        """生成确定性工件引用并保留输入供断言归档次序。"""
        self.saved.append(message)
        return {
            "artifact_id": f"artifact-{message.tool_call_id}",
            "source_turn_id": context.turn_id,
        }


def budget():
    """压缩触发与总容量分别配置，确保摘要模型有自己的合法输入空间。"""
    return ContextBudget(
        model_input_limit=60_000,
        reserved_output_tokens=1000,
        system_policy_reserve=0,
        tool_schema_reserve=0,
        safety_margin=100,
        recent_turns=0,
        summary_trigger_tokens=1000,
        soft_input_tokens=2000,
    )


def draft():
    """包含否定、日期、金额及待确认状态的结构化测试摘要。"""
    return WorkingContextDraft(
        goal="继续比较，不执行交易",
        scope="本次研究",
        constraints=("金额不超过1000元",),
        decisions=("2026-09-12的数据仅为历史证据",),
        completed_steps=("已查询第一组行情",),
        pending_questions=("风险画像仍待用户确认",),
        next_steps=("比较剩余结果",),
    ).model_dump_json()


def context():
    """无业务SQL身份替身，仅测试公开 native runtime 的受信任上下文。"""
    return ExecutionContext(tenant_id="tenant", subject_id="subject", turn_id="turn", scopes=set())


def messages():
    """同一轮两次已完成执行，较早中段足够大以触发实际压缩。"""
    return [
        HumanMessage(content="请继续比较，不执行交易", id="real-user"),
        AIMessage(
            content="", id="old-call", tool_calls=[{"name": "lookup", "id": "first", "args": {}}]
        ),
        ToolMessage(
            content="历史明细" * 1000, id="old-result", name="lookup", tool_call_id="first"
        ),
        AIMessage(
            content="", id="last-call", tool_calls=[{"name": "lookup", "id": "last", "args": {}}]
        ),
        ToolMessage(content="最近必要结果", id="last-result", name="lookup", tool_call_id="last"),
    ]


def middleware(*, reader=None, summary=None):
    """装配真实压缩适配器和仅替换存储传输的归档探针。"""
    result = NativeContextMiddleware(
        budget(),
        summary_model=summary or BoundFakeModel(responses=[AIMessage(content=draft())]),
        privacy_epoch_reader=reader,
    )
    result.archive = Archive()
    return result


def persistent_saver(root: Path):
    """使用官方公开 PersistentDict factory 重新打开 native checkpoint，而非手工注入最终state。"""
    index = 0

    def factory(*args):
        """每个native存储字典拥有独立文件，重建进程对象时显式加载。"""
        nonlocal index
        filename = root / f"checkpoint-{index}.pkl"
        index += 1
        result = PersistentDict(*args, filename=str(filename))
        if filename.exists():
            result.load()
        return result

    return InMemorySaver(factory=factory)


def test_same_turn_compaction_survives_native_checkpoint_reconstruction(tmp_path):
    """S42/S45：原生Agent持久化中段移除与规范摘要，新Agent从磁盘恢复且不重放工具。"""
    config = {"configurable": {"thread_id": "native-resume"}}
    original = messages()
    first = middleware()
    with persistent_saver(tmp_path) as saver:
        agent = create_agent(
            BoundFakeModel(responses=[AIMessage(content="比较完成")]),
            middleware=[first, FinalContextMiddleware(RequestRecorder(budget()))],
            context_schema=ExecutionContext,
            state_schema=ConversationState,
            checkpointer=saver,
        )
        result = agent.invoke({"messages": original}, config, context=context())
        assert result["messages"][0] == original[0]
        assert "old-result" not in [item.id for item in result["messages"]]
        assert result["working_context"]["summary_version"] == 1
        assert result["working_context"]["pending_questions"] == ["风险画像仍待用户确认"]
        assert completed_tool_batches(result["messages"]) is not None
        assert first.archive.saved[0].content == original[2].content
        assert not any(
            item.additional_kwargs.get("lc_source") == "summarization"
            for item in result["messages"]
        )
    with persistent_saver(tmp_path) as saver:
        rebuilt = create_agent(
            BoundFakeModel(responses=[AIMessage(content="恢复完成")]),
            middleware=[middleware()],
            context_schema=ExecutionContext,
            state_schema=ConversationState,
            checkpointer=saver,
        )
        state = rebuilt.get_state(config)
        assert state.values["working_context"] == result["working_context"]
        assert current_turn_start(state.values["messages"], "real-user") == 0
        resumed = rebuilt.invoke(None, config, context=context())
        assert resumed["messages"][0] == original[0]
        assert (
            resumed["working_context"]["evidence_refs"]
            == result["working_context"]["evidence_refs"]
        )


def test_parallel_partial_batch_and_privacy_change_protect_recovery():
    """S43/S49：未配对并行批次不摘要；隐私版本在摘要期间变化则结果作废。"""
    original = messages()
    original[-2] = AIMessage(
        content="",
        id="last-call",
        tool_calls=[
            {"name": "lookup", "id": "last", "args": {}},
            {"name": "lookup", "id": "pending", "args": {}},
        ],
    )
    partial = middleware()
    update = partial.before_model({"messages": original}, Runtime(context=context()))
    assert "messages" not in update and not partial.archive.saved
    epochs = iter([1, 1, 1, 1, 2])
    changed = middleware(reader=lambda _: next(epochs))
    update = changed.before_model(
        {"messages": messages(), "context_privacy_epoch": 1}, Runtime(context=context())
    )
    assert update["context_compaction_error"] == "ValueError"
    assert "messages" not in update and "working_context" not in update


def test_privacy_change_during_archiving_blocks_summary_transmission(monkeypatch):
    """归档发生隐私变更时，旧上下文不得在随后真正发给摘要Provider。"""
    epoch = [1]
    summary = BoundFakeModel(responses=[AIMessage(content=draft()), AIMessage(content="unused")])
    handler = middleware(reader=lambda _: epoch[0], summary=summary)
    original = handler.archive.save

    def archive_then_forget(message, context):
        """在确定的归档边界推进owner隐私版本，模拟另一个会话遗忘。"""
        result = original(message, context)
        epoch[0] = 2
        return result

    monkeypatch.setattr(handler.archive, "save", archive_then_forget)
    update = handler.before_model(
        {"messages": messages(), "context_privacy_epoch": 1}, Runtime(context=context())
    )
    assert update["context_compaction_error"] == "ValueError"
    assert summary.i == 0
    assert "messages" not in update


def test_summary_failure_fingerprint_is_bounded_and_retains_raw_state():
    """S44：非法摘要不能变成假工作状态，同输入最多尝试两次。"""
    broken = middleware(summary=BoundFakeModel(responses=[AIMessage(content="invalid json")]))
    state = {"messages": messages()}
    for _ in range(4):
        state.update(broken.before_model(state, Runtime(context=context())))
    assert state["context_compaction_attempts"] == 2
    assert state["summary_calls"] == 2
    assert state["messages"][2].content == messages()[2].content
    assert not state.get("working_context")


def test_privacy_reset_keeps_business_receipts_and_invalidates_summary():
    """S36：删除只清除派生解释，原始用户输入和真实工具执行凭据继续可用。"""
    handler = middleware(reader=lambda _: 2)
    original = messages()
    original.insert(3, AIMessage(content="由旧秘密画像推断的解释", id="tainted"))
    state = {
        "messages": original,
        "context_privacy_epoch": 1,
        "working_context": {"goal": "旧秘密"},
    }
    update = handler.before_model(state, Runtime(context=context()))
    result = add_messages(original, update["messages"])
    assert update["working_context"] is None
    assert result[0] == original[0]
    assert [item.tool_call_id for item in result if isinstance(item, ToolMessage)] == [
        "first",
        "last",
    ]
    assert "旧秘密" not in str(result)


def test_native_interrupt_resume_preserves_pending_tool_batch(tmp_path):
    """S43/S45：真实native interrupt跨checkpoint重建后恢复，待答调用不被摘要移除。"""
    executions = []

    @tool
    def ask_user() -> str:
        """请求必要用户补充，测试恢复后才记录实际业务动作。"""
        answer = interrupt({"question": "确认继续？"})
        executions.append(answer)
        return str(answer)

    config = {"configurable": {"thread_id": "interrupt-resume"}}
    call = AIMessage(content="", tool_calls=[{"name": "ask_user", "id": "pending", "args": {}}])
    with persistent_saver(tmp_path) as saver:
        agent = create_agent(
            BoundFakeModel(responses=[call]),
            tools=[ask_user],
            middleware=[middleware()],
            context_schema=ExecutionContext,
            state_schema=ConversationState,
            checkpointer=saver,
        )
        stopped = agent.invoke(
            {"messages": [HumanMessage(content="继续", id="real-user")]}, config, context=context()
        )
        assert stopped["__interrupt__"] and not executions
    with persistent_saver(tmp_path) as saver:
        agent = create_agent(
            BoundFakeModel(responses=[AIMessage(content="已经得到补充")]),
            tools=[ask_user],
            middleware=[middleware()],
            context_schema=ExecutionContext,
            state_schema=ConversationState,
            checkpointer=saver,
        )
        result = agent.invoke(Command(resume="继续"), config, context=context())
        assert executions == ["继续"]
        assert result["messages"][0].id == "real-user"
        assert completed_tool_batches(result["messages"]) is not None


def test_agent_factory_executes_compaction_as_native_state_update():
    """S11-0：正式AgentFactory装配运行摘要节点，输出仍从正确用户锚点识别。"""
    from financeclaw.agent_server.agents.factory import AgentFactory
    from financeclaw.agent_server.tools.catalog import ToolCatalog
    from financeclaw.agent_server.tools.policy import ToolPolicy
    from financeclaw.kernel.agents import AgentProfile
    from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef
    from financeclaw.shared.audit.repository import InMemoryAuditRepository
    from financeclaw.shared.llm.factory import ModelFactory

    ref = ModelProfileRef(profile_id="test", version="1.0.0")
    profiles = ModelProfileCatalog(
        (
            ModelProfile(
                profile_id="test",
                version="1.0.0",
                model="openai:test",
                context_window_tokens=60_000,
                max_tokens=1000,
            ),
        )
    )
    factory = AgentFactory(
        model_factory=ModelFactory(profiles, api_key=None, base_url=None),
        tool_catalog=ToolCatalog(()),
        tool_policy=ToolPolicy(),
        audit=InMemoryAuditRepository(),
        debug_full_io=False,
        context_budget=budget(),
    )
    profile = AgentProfile(
        agent_id="context-probe",
        version="1.0.0",
        model_profile=ref,
        system_prompt_template="Complete the current bounded task.",
        allowed_tools=(),
    )
    agent = factory.build(
        profile,
        model=BoundFakeModel(
            responses=[
                AIMessage(content=draft()),
                AIMessage(content="实际Factory继续成功"),
            ]
        ),
    )
    original = [
        HumanMessage(content="旧历史", id="old-user"),
        AIMessage(content="历史明细" * 1500, id="old-answer"),
        HumanMessage(content="继续比较", id="real-user"),
    ]
    result = agent.invoke(
        {"messages": original},
        {"configurable": {"thread_id": "factory-probe"}},
        context=context(),
    )
    assert result["working_context"]["summary_version"] == 1
    assert result["messages"][0] == original[-1]
    assert result["messages"][-1].content == "实际Factory继续成功"
    assert current_turn_start(result["messages"], "real-user") == 0
