"""使用真实 AgentFactory、原生 Command 和 checkpoint 验证技能激活。"""

from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from financeclaw.agent_server.context.planning import completed_tool_batches
from financeclaw.kernel.skills import SkillError
from financeclaw.kernel.turns import is_user_message
from financeclaw.shared.skills.access import ACCESS_KEY
from tests.stage1.test_agent import components_with_tools, context
from tests.stage11.test_context_native import BoundFakeModel


class RecordingModel(BoundFakeModel):
    """替换模型传输并保留每次实际输入及工具绑定。"""

    requests: ClassVar[list] = []
    bindings: ClassVar[list] = []

    def bind_tools(self, tools, **kwargs):
        """记录最终收尾阶段实际关闭的工具集合。"""
        type(self).bindings.append([getattr(t, "name", str(t)) for t in tools])
        return self

    def _generate(self, messages, *args, **kwargs):
        """在实际模型边界抓取输入；不从最终 state 推测 prompt。"""
        type(self).requests.append(messages)
        return super()._generate(messages, *args, **kwargs)


def call(name, arguments, identity):
    """生成框架真实分派的工具调用。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": identity}])


def setup(responses, **profile_overrides):
    """使用生产工厂和固定发布，仅替换外部模型。"""
    components, audit = components_with_tools()
    profile = components.default_agent_profile.model_copy(update=profile_overrides)
    RecordingModel.requests, RecordingModel.bindings = [], []
    graph = components.agent_factory.build(profile, model=RecordingModel(responses=responses))
    return graph, audit


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.asyncio
async def test_load_and_resource_are_native_and_body_is_request_only(asynchronous, explicit):
    """S06/S14/S15/S22：显式首轮与模型选择共用准备，正文只出现于真实请求。"""
    responses = [] if explicit else [call("load_skill", {"skill_id": "market-brief"}, "load")]
    responses += [
        call(
            "read_skill_resource",
            {"skill_id": "market-brief", "resource_path": "references/report-format.md"},
            "resource",
        ),
        AIMessage(content="完成"),
    ]
    graph, audit = setup(responses)
    config = {"configurable": {"thread_id": f"load-{explicit}-{asynchronous}"}}
    inputs = {
        "messages": [
            HumanMessage(
                content=("/skill market-brief " if explicit else "") + "请整理 AAPL 简报",
                id="real-user",
            )
        ]
    }
    kwargs = {"config": config, "context": context("*")}
    result = (
        await graph.ainvoke(inputs, **kwargs) if asynchronous else graph.invoke(inputs, **kwargs)
    )
    assert len(result["skill_state"]["active"]) == 1
    assert result["skill_state"]["explicit_initialization_done"]
    assert completed_tool_batches(result["messages"]) is not None
    assert all(
        m.additional_kwargs.get("financeclaw_content_kind") != "skill_instructions"
        for m in result["messages"]
    )
    for i, request in enumerate(RecordingModel.requests):
        bodies = [
            m
            for m in request
            if m.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
        ]
        assert len(bodies) == (1 if explicit or i > 0 else 0)
        assert all(not is_user_message(m) for m in bodies)
    resource = next(
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == "read_skill_resource"
    )
    assert resource.status == "success"
    assert resource.additional_kwargs[ACCESS_KEY]
    assert result["messages"][-1].additional_kwargs[ACCESS_KEY]
    assert any(e.event_type.value == "skill.load_prepared" for e in audit.records())
    assert any(e.event_type.value == "skill.resource_read" for e in audit.records())


def test_exclusive_batch_rejects_before_any_side_effect():
    """S07：混合 load 与业务工具的整批均拒绝，不能部分执行。"""
    mixed = AIMessage(
        content="",
        tool_calls=[
            {"name": "load_skill", "args": {"skill_id": "market-brief"}, "id": "load"},
            {"name": "market_snapshot", "args": {"symbol": "AAPL"}, "id": "quote"},
        ],
    )
    graph, audit = setup([mixed, AIMessage(content="请单独加载")])
    result = graph.invoke(
        {"messages": [HumanMessage(content="整理简报", id="user")]},
        {"configurable": {"thread_id": "batch"}},
        context=context("*"),
    )
    assert result["skill_state"]["active"] == []
    assert (
        len([m for m in result["messages"] if isinstance(m, ToolMessage) and m.status == "error"])
        == 2
    )
    assert not any(e.decision == "executed" for e in audit.records())


def test_duplicate_loading_and_new_turn_cleanup():
    """S08/S10/S20/S25：重放加载幂等，新轮清理派生正文同时保持批次配对。"""
    graph, _ = setup(
        [
            call("load_skill", {"skill_id": "market-brief"}, "a"),
            call("load_skill", {"skill_id": "market-brief"}, "b"),
            AIMessage(content="OLD_SECRET"),
        ]
    )
    config = {"configurable": {"thread_id": "reuse"}}
    result = graph.invoke(
        {"messages": [HumanMessage(content="整理简报", id="u1")]},
        config,
        context=context("*", turn_id="first"),
    )
    assert len(result["skill_state"]["active"]) == 1
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert "already_active" in receipts[-1].content
    graph2 = graph
    # 状态更新模拟下一轮只需要普通回复的模型，不更改 checkpoint。
    original = RecordingModel.requests.copy()
    with pytest.raises(SkillError):
        graph2.invoke({"messages": []}, config, context=context("tools:read", turn_id="first"))
    assert len(RecordingModel.requests) == len(original)


def test_new_turn_removes_old_derived_answer_before_model():
    """S20：下一轮不再激活，实际输入不含旧回复、资料或工具参数。"""
    graph, _ = setup([AIMessage(content="OLD_SECRET"), AIMessage(content="普通回复")])
    config = {"configurable": {"thread_id": "two-turns"}}
    graph.invoke(
        {"messages": [HumanMessage(content="/skill market-brief 简报", id="u1")]},
        config,
        context=context("*", turn_id="one"),
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content="新任务", id="u2")]},
        config,
        context=context("*", turn_id="two"),
    )
    assert result["skill_state"]["active"] == []
    assert "OLD_SECRET" not in str(RecordingModel.requests[-1])
    assert len([m for m in result["messages"] if is_user_message(m)]) == 2


def test_finish_closes_tools_and_keeps_skill_body():
    """S26：最后模型额度关闭全部工具，技能正文及收尾提示仍进入实际请求。"""
    graph, _ = setup([AIMessage(content="尚未读取所需资料，无法给出完整结论")], max_model_calls=1)
    result = graph.invoke(
        {"messages": [HumanMessage(content="/skill market-brief 简报", id="u1")]},
        {"configurable": {"thread_id": "finish"}},
        context=context("*"),
    )
    assert result["finishing"]
    assert not RecordingModel.bindings or not RecordingModel.bindings[-1]
    assert any("final answer allowance" in str(m.content) for m in RecordingModel.requests[-1])
    assert (
        sum(
            m.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
            for m in RecordingModel.requests[-1]
        )
        == 1
    )


def test_hitl_resume_and_process_reconstruction_keep_exact_activation(tmp_path):
    """S10/S16：原生暂停/磁盘 checkpoint 重建后不重选或重复加载，不绕过写审批。"""
    from langgraph.types import Command

    from financeclaw.agent_server.tools.local import WatchlistWriteTool
    from tests.stage11.test_context_native import persistent_saver

    writer = WatchlistWriteTool()
    components, _ = components_with_tools(write=writer)
    profile = components.default_agent_profile
    config = {"configurable": {"thread_id": "restart"}}
    RecordingModel.requests, RecordingModel.bindings = [], []
    with persistent_saver(tmp_path) as saver:
        graph = components.agent_factory.build(
            profile,
            checkpointer=saver,
            model=RecordingModel(
                responses=[call("watchlist_add", {"symbol": "AAPL", "note": "test"}, "write")]
            ),
        )
        result = graph.invoke(
            {"messages": [HumanMessage(content="/skill market-brief 加入自选", id="u")]},
            config,
            context=context("*"),
            version="v2",
        )
        assert result.interrupts and not writer.writes
        binding = graph.get_state(config).values["skill_state"]
    with persistent_saver(tmp_path) as saver:
        graph = components.agent_factory.build(
            profile, checkpointer=saver, model=RecordingModel(responses=[AIMessage(content="完成")])
        )
        resumed = graph.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}), config, context=context("*")
        )
        assert len(writer.writes) == 1
        assert resumed["skill_state"]["active"] == binding["active"]
        assert len(resumed["skill_state"]["active"]) == 1
        assert (
            sum(
                m.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
                for m in RecordingModel.requests[-1]
            )
            == 1
        )


def test_failed_explicit_prepare_commits_cost_without_activation(monkeypatch):
    """S21/S25：显式候选失败仍将真实摘要成本写入 checkpoint，然后受控结束。"""
    from financeclaw.agent_server.context.preparation import CandidatePreparation

    def fail(candidate, runtime):
        """只模拟候选最终容量失败，保留已真实消耗的计量。"""
        error = SkillError("SKILL_CONTEXT_BUDGET_EXCEEDED")
        error.state_update = {"summary_calls": 1, "context_compaction_attempts": 1}
        raise error

    monkeypatch.setattr(
        CandidatePreparation, "prepare", lambda self, state, runtime: fail(state, runtime)
    )
    graph, _ = setup([AIMessage(content="不得被调用")])
    result = graph.invoke(
        {"messages": [HumanMessage(content="/skill market-brief 简报", id="user")]},
        {"configurable": {"thread_id": "explicit-fail"}},
        context=context("*"),
    )
    assert result["summary_calls"] == 1
    assert result["skill_state"]["active"] == []
    assert not result["skill_state"]["explicit_initialization_done"]
    assert result["skill_state"]["preparation_error"]["code"] == "SKILL_CONTEXT_BUDGET_EXCEEDED"
    assert not RecordingModel.requests


def test_retry_rechecks_live_authorization_before_transmission(monkeypatch):
    """S23：第一次供应商错误后撤权，框架重试不能再把技能内容发送给供应商。"""
    from financeclaw.agent_server.skills.service import SkillService

    permission = {"allowed": True}
    original = SkillService.validate_request

    def validate(service, runtime, state, messages):
        """模拟外部授权变化，保留其余真实发布与来源复验。"""
        if not permission["allowed"]:
            raise SkillError()
        return original(service, runtime, state, messages)

    class RevokingModel(RecordingModel):
        """供应商首次接收请求后撤销权限并制造瞬态失败。"""

        def _generate(self, messages, *args, **kwargs):
            """后续重试若进入传输，计数会暴露失败。"""
            type(self).requests.append(messages)
            permission["allowed"] = False
            raise TimeoutError("synthetic provider timeout")

    monkeypatch.setattr(SkillService, "validate_request", validate)
    components, _ = components_with_tools()
    RevokingModel.requests = []
    graph = components.agent_factory.build(
        components.default_agent_profile, model=RevokingModel(responses=[])
    )
    with pytest.raises(SkillError, match="无法使用"):
        graph.invoke(
            {"messages": [HumanMessage(content="/skill market-brief 简报", id="u")]},
            {"configurable": {"thread_id": "retry"}},
            context=context("*"),
        )
    assert len(RevokingModel.requests) == 1


def test_successful_load_is_not_relabelled_by_an_older_error():
    """S21：Command 带回既有历史时，审计和进度只判断当前 call ID。"""
    graph, audit = setup(
        [
            call("load_skill", {"skill_id": "missing"}, "bad"),
            call("load_skill", {"skill_id": "market-brief"}, "good"),
            AIMessage(content="完成"),
        ]
    )
    events = list(
        graph.stream(
            {"messages": [HumanMessage(content="简报", id="user")]},
            {"configurable": {"thread_id": "old-error"}},
            context=context("*"),
            stream_mode="custom",
        )
    )
    statuses = [e["status"] for e in events if e.get("type") == "tool.progress"]
    assert statuses == ["started", "failed", "started", "completed"]
    tool_results = [
        e.decision for e in audit.records() if e.event_type.value.startswith("financial_tool.")
    ]
    assert tool_results == ["failed", "executed"]


def test_fallback_keeps_one_body_on_each_actual_request():
    """S14/S23：fallback 包装位于技能投影内部，每次真实请求都复验但不重复正文。"""

    class FailingModel(RecordingModel):
        """第一次供应商仅制造传输失败，不改变授权。"""

        def _generate(self, messages, *args, **kwargs):
            """记录实际首个请求供跨供应商比较。"""
            type(self).requests.append(messages)
            raise TimeoutError("synthetic timeout")

    components, _ = components_with_tools()
    components.agent_factory.model_max_retries = 0
    FailingModel.requests, RecordingModel.requests = [], []
    graph = components.agent_factory.build(
        components.default_agent_profile,
        model=FailingModel(responses=[]),
        fallback_models=(RecordingModel(responses=[AIMessage(content="完成")]),),
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content="/skill market-brief AAPL", id="user")]},
        {"configurable": {"thread_id": "fallback"}},
        context=context("*"),
    )
    assert result["messages"][-1].content == "完成"
    for request in [*FailingModel.requests, *RecordingModel.requests]:
        assert (
            sum(
                m.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
                for m in request
            )
            == 1
        )
    assert len(FailingModel.requests) == len(RecordingModel.requests) == 1
