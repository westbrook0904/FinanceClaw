"""原生压缩与工具回读的行为测试，覆盖真实 reducer 和中间件。"""

from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from financeclaw.agent_server.context.budget import ContextBudget
from financeclaw.agent_server.context.compaction import NativeContextMiddleware
from financeclaw.agent_server.middleware.artifact_middleware import ToolResultArtifactMiddleware
from financeclaw.agent_server.middleware.context_editing import ToolContextEditingMiddleware
from financeclaw.agent_server.middleware.final_context import (
    FinalContextMiddleware,
    RequestRecorder,
)
from financeclaw.kernel.turns import current_turn_start


def budget(**updates):
    """小阈值使合成测试实际触发压缩，容量仍有充分余量。"""
    return ContextBudget(
        model_input_limit=20000,
        reserved_output_tokens=512,
        system_policy_reserve=0,
        tool_schema_reserve=0,
        safety_margin=128,
        recent_turns=0,
        summary_trigger_tokens=256,
        **updates,
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_summary_preserves_current_turn_and_archives_old_result(memory_stack, asynchronous):
    """旧结果归档，当前完整工具后缀原样保留，摘要没有冒充真实用户。"""
    context, identity, repository, artifacts, _, _ = memory_stack
    current = [
        HumanMessage(content="继续比较", id=identity),
        AIMessage(
            content="",
            id="call",
            tool_calls=[{"id": "new-call", "name": "unknown_mcp", "args": {}}],
        ),
        ToolMessage(
            content="当前完整结果", id="new-result", tool_call_id="new-call", name="unknown_mcp"
        ),
    ]
    old = [
        HumanMessage(content="旧问题", id="old-user"),
        AIMessage(
            content="",
            id="old-ai",
            tool_calls=[{"id": "old-call", "name": "unknown_mcp", "args": {}}],
        ),
        ToolMessage(
            content="旧数据" * 200, id="old-result", tool_call_id="old-call", name="unknown_mcp"
        ),
        AIMessage(content="旧结论", id="old-final"),
    ]
    state = {"messages": old + current}
    middleware = NativeContextMiddleware(
        budget(),
        repository,
        artifacts,
        FakeMessagesListChatModel(responses=[AIMessage(content="旧决定和未决事项")]),
    )
    runtime = Runtime(context=context)
    update = (
        await middleware.abefore_model(state, runtime)
        if asynchronous
        else middleware.before_model(state, runtime)
    )
    result = add_messages(state["messages"], update["messages"])
    assert result[-len(current) :] == current
    assert current_turn_start(result, identity) == 1
    assert result[0].additional_kwargs["lc_source"] == "summarization"
    catalog = artifacts.repository.list_turn(
        context.conversation_id, context.turn_id, context.tenant_id, context.subject_id
    )
    assert len(catalog) == 1
    owned = context.model_copy(update={"scopes": {*context.scopes, "artifacts:read"}})
    assert "旧数据" in artifacts.read(catalog[0].artifact_id, context=owned).decode()
    assert repository.list_manifests(context.conversation_id)[0].subtype == "summary"


def test_unknown_tool_gets_default_archive_and_small_result_survives_cleanup(memory_stack):
    """无任何保留标注的工具也能归档；小结果清理前才保存。"""
    context, identity, repository, artifacts, _, _ = memory_stack
    request = SimpleNamespace(
        runtime=Runtime(context=context), tool_call={"name": "external_mcp", "id": "call"}
    )
    raw = ToolMessage(content="外部数据" * 300, tool_call_id="call", name="external_mcp")
    middleware = ToolResultArtifactMiddleware(artifacts)
    projected = middleware._project(request, raw)
    assert projected.additional_kwargs["artifact_ref"]
    assert middleware._project(request, raw).artifact == projected.artifact
    small = ToolMessage(content="精确小结果 42.58", tool_call_id="small", name="external_mcp")
    assert middleware._project(request, small).content == small.content
    messages = [
        HumanMessage(content="查数据" * 200, id=identity),
        AIMessage(content="", tool_calls=[{"id": "small", "name": "external_mcp", "args": {}}]),
        small,
    ]
    model = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])
    edit = ToolContextEditingMiddleware(
        artifacts, budget(soft_input_tokens=256, tool_results_to_keep=0)
    )
    req = ModelRequest(
        model=model,
        messages=messages,
        tools=[],
        runtime=request.runtime,
        state={"messages": messages},
    )
    changed = edit.wrap_model_call(req, lambda value: value)
    assert changed.messages[-1].additional_kwargs["artifact_ref"]
    assert messages[-1].content == "精确小结果 42.58"


def test_final_guard_does_not_empty_user_input(memory_stack):
    """超硬容量明确失败，原始用户输入不被修改。"""
    context, identity, _, _, _, _ = memory_stack
    user = HumanMessage(content="原始用户输入" * 10000, id=identity)
    req = ModelRequest(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="ok")]),
        messages=[user],
        tools=[],
        runtime=Runtime(context=context),
        state={"messages": [user]},
    )
    guard = FinalContextMiddleware(RequestRecorder(budget()))
    with pytest.raises(ValueError, match="input budget"):
        guard.wrap_model_call(req, lambda value: pytest.fail("model must not run"))
    assert user.content == "原始用户输入" * 10000


def test_summary_failure_keeps_original_messages(memory_stack):
    """摘要失败不生成假摘要，是否可继续由最终硬预算决定。"""
    context, identity, repository, artifacts, _, _ = memory_stack

    class Broken(FakeMessagesListChatModel):
        """确定性模拟摘要供应商不可用。"""

        def _generate(self, *args, **kwargs):
            """所有摘要尝试均失败，验证原 state 不被覆盖。"""
            raise ValueError("summary unavailable")

    messages = [
        HumanMessage(content="旧" * 2000, id="old"),
        AIMessage(content="结论", id="old-answer"),
        HumanMessage(content="当前", id=identity),
    ]
    middleware = NativeContextMiddleware(budget(), repository, artifacts, Broken(responses=[]))
    result = middleware.before_model({"messages": messages}, Runtime(context=context))
    assert result["context_compaction_error"] == "ValueError"
    assert "messages" not in result
    assert messages[0].content == "旧" * 2000
    manifests = repository.list_manifests(context.conversation_id)
    assert len(manifests) == 3 and all(item.subtype == "summary" for item in manifests)


def test_bootstrap_reads_only_completed_pairs_once(memory_stack, monkeypatch):
    """更换 thread 时只初始化完成问答，失败 Turn 不进入新 state。"""
    context, identity, repository, artifacts, _, _ = memory_stack
    from tests.turn_support import finish_turn, seed_execution

    finish_turn(repository, context.turn_id)
    answer = repository.append_assistant_message(turn_id=context.turn_id, content="已完成的答案")
    failed = seed_execution(
        repository.execution,
        context.model_copy(update={"turn_id": "failed", "command_id": None}),
        {"limits": {"model": 100, "tool": 100, "command": 100}},
        message="未完成的问题",
    )
    failed_user = repository.list_messages(context.conversation_id)[-1]
    finish_turn(repository, failed.turn_id, "failed")
    current = seed_execution(
        repository.execution,
        context.model_copy(update={"turn_id": "new", "command_id": None}),
        {"limits": {"model": 100, "tool": 100, "command": 100}},
        message="新问题",
    )
    user = repository.list_messages(context.conversation_id)[-1]
    monkeypatch.setattr(
        "financeclaw.agent_server.context.compaction.user_anchor", lambda *_: user.message_id
    )

    def forbidden_scan(*args, **kwargs):
        """任何无界整会话读取都使本场景失败。"""
        pytest.fail("normal context must not scan all Journal messages")

    monkeypatch.setattr(repository, "list_messages", forbidden_scan)
    middleware = NativeContextMiddleware(
        budget().model_copy(update={"recent_turns": 4}), repository, artifacts
    )
    state = {"messages": [HumanMessage(content=user.content, id=user.message_id)]}
    update = middleware.before_agent(state, Runtime(context=current))
    state["messages"] = add_messages(state["messages"], update["messages"])
    state["context_bootstrapped"] = update["context_bootstrapped"]
    assert [item.id for item in state["messages"]] == [identity, answer.message_id, user.message_id]
    assert failed_user.message_id not in [item.id for item in state["messages"]]
    assert middleware.before_agent(state, Runtime(context=current)) is None


def test_archive_failure_cannot_discard_unique_tool_result(memory_stack, monkeypatch):
    """摘要完成也不能越过归档失败；旧唯一工具原文保持不变。"""
    context, identity, repository, artifacts, _, _ = memory_stack
    raw = ToolMessage(content="唯一原始明细" * 500, id="raw", name="mcp", tool_call_id="call")
    messages = [
        HumanMessage(content="旧问题", id="old"),
        AIMessage(content="", tool_calls=[{"name": "mcp", "id": "call", "args": {}}]),
        raw,
        AIMessage(content="旧答复"),
        HumanMessage(content="追问", id=identity),
    ]

    def unavailable(*args, **kwargs):
        """模拟对象存储持久化失败。"""
        raise ConnectionError("artifact store unavailable")

    monkeypatch.setattr(artifacts, "persist", unavailable)
    middleware = NativeContextMiddleware(
        budget(),
        repository,
        artifacts,
        FakeMessagesListChatModel(responses=[AIMessage(content="摘要")]),
    )
    with pytest.raises(ConnectionError):
        middleware.before_model({"messages": messages}, Runtime(context=context))
    assert messages[2] is raw and raw.content == "唯一原始明细" * 500


def test_successful_forget_resets_mixed_summary_and_prior_recall(memory_stack):
    """保留用户删除请求与执行回执，不让混合摘要和本轮中间解释继续携带旧事实。"""
    context, identity, repository, artifacts, _, _ = memory_stack
    messages = [
        HumanMessage(
            content="旧秘密画像", id="summary", additional_kwargs={"lc_source": "summarization"}
        ),
        HumanMessage(content="删除此前记忆", id=identity),
        AIMessage(content="", tool_calls=[{"name": "search_memories", "args": {}, "id": "recall"}]),
        ToolMessage(content="旧秘密画像", name="search_memories", tool_call_id="recall"),
        AIMessage(
            content="根据旧秘密画像执行删除",
            tool_calls=[
                {
                    "name": "forget_memory",
                    "args": {"memory_id": "profile:language", "mode": "delete"},
                    "id": "forget",
                }
            ],
        ),
        ToolMessage(content='{"status":"deleted"}', name="forget_memory", tool_call_id="forget"),
    ]
    middleware = NativeContextMiddleware(budget(), repository, artifacts)
    update = middleware.before_model(
        {"messages": messages, "memory_forget_requested": True}, Runtime(context=context)
    )
    result = add_messages(messages, update["messages"])
    assert [message.type for message in result] == ["human", "ai", "tool"]
    assert result[0].id == identity and result[-1].tool_call_id == "forget"
    assert "旧秘密画像" not in str([message.content for message in result])
