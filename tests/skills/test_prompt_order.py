"""技能正文在摘要与历史轮次之后投影，预算与真实模型请求保持相同边界。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from financeclaw.agent_server.context.planning import completed_tool_batches, projected_messages
from financeclaw.agent_server.context.state import WorkingContext
from tests.skills.test_governance import activate, service_state
from tests.skills.test_runtime import RecordingModel, call, setup
from tests.stage1.test_agent import context


def summary():
    """只构造旧轮次摘要，不把技能正文或新用户输入放入摘要。"""
    return WorkingContext(
        goal="更早的历史讨论",
        summary_version=1,
        source_boundary="older-answer",
        input_fingerprint="a" * 64,
    ).model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_model_receives_history_then_skill_then_current_turn(asynchronous, explicit):
    """显式或工具加载、同步或异步，每次真实调用都保持历史完整且不拆本轮工具往返。"""
    responses = [] if explicit else [call("load_skill", {"skill_id": "market-brief"}, "load")]
    responses += [
        call(
            "read_skill_resource",
            {"skill_id": "market-brief", "resource_path": "references/report-format.md"},
            "resource",
        ),
        AIMessage(content="完成"),
    ]
    graph, _ = setup(responses)
    current = ("/skill market-brief " if explicit else "") + "整理 AAPL 简报"
    inputs = {
        "working_context": summary(),
        "messages": [
            HumanMessage(content="历史问题", id="past-user"),
            AIMessage(content="历史回答", id="past-answer"),
            HumanMessage(content=current, id="current-user"),
        ],
    }
    kwargs = {
        "config": {"configurable": {"thread_id": f"prompt-order-{asynchronous}-{explicit}"}},
        "context": context("*"),
    }
    result = (
        await graph.ainvoke(inputs, **kwargs) if asynchronous else graph.invoke(inputs, **kwargs)
    )
    for index, messages in enumerate(RecordingModel.requests):
        ids = [message.id for message in messages]
        assert ids.index("working-context-1") < ids.index("past-user")
        assert ids.index("past-user") < ids.index("past-answer") < ids.index("current-user")
        bodies = [
            message
            for message in messages
            if message.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
        ]
        assert len(bodies) == (1 if explicit or index > 0 else 0)
        if bodies:
            assert ids.index("past-answer") < ids.index(bodies[0].id) < ids.index("current-user")
        assert (
            next(message for message in messages if message.id == "current-user").content == current
        )
        assert completed_tool_batches(messages) is not None
    assert result["skill_state"]["user_message_id"] == "current-user"
    assert all(
        message.additional_kwargs.get("financeclaw_content_kind") != "skill_instructions"
        for message in result["messages"]
    )


def test_budget_and_resume_keep_original_turn_anchor_before_clarification():
    """同轮补充消息不会把技能挪到中途，纯预算投影也使用冻结的原用户输入位置。"""
    service, runtime, state = service_state()
    state = activate(service, runtime, state)
    state["working_context"] = summary()
    state["messages"] = [
        HumanMessage(content="历史问题", id="past-user"),
        AIMessage(content="历史回答", id="past-answer"),
        *state["messages"],
        AIMessage(content="请补充日期", id="question"),
        HumanMessage(content="今天", id="clarification"),
    ]
    state["skill_state"] = service.binding(runtime, state)
    assert state["skill_state"]["user_message_id"] == "user"
    messages = projected_messages(state, skill_projection=service.projection)
    ids = [message.id for message in messages]
    body = next(
        message
        for message in messages
        if message.additional_kwargs.get("financeclaw_content_kind") == "skill_instructions"
    )
    assert ids.index("working-context-1") < ids.index("past-user") < ids.index("past-answer")
    assert ids.index("past-answer") < ids.index(body.id) < ids.index("user")
    assert ids.index("user") < ids.index("question") < ids.index("clarification")
