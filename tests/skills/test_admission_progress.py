"""受理固定选择、准备流以及飞书渠道的有界错误与幂等卡片。"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph_sdk.schema import StreamPart
from sqlalchemy import func, select

from financeclaw.api.application.turns.answer_stream import TurnAnswerStream, consume
from financeclaw.kernel.skills import SkillError, SkillPreparation
from financeclaw.shared.notifications.tables import NotificationEventRow
from financeclaw.shared.skills.catalog import SkillCatalog
from tests.skills.test_runtime import setup
from tests.stage1.test_agent import context
from tests.stage8_hotfix.test_feishu_cards import publish
from tests.stage8_hotfix.test_feishu_clarification import Replies, channel, message
from tests.stage8_hotfix.test_feishu_streaming import started
from tests.stage10.conftest import admit as admit
from tests.stage10.conftest import service as service
from tests.stage10.runtime import final_state, tick
from tests.stage10.runtime import runtime as runtime


def test_admission_pins_choice_and_replay_does_not_reparse(service, admit):
    """S04/S08：固定选择写入原受理快照；相同幂等键只返回原 Turn。"""
    accepted = admit(message="/skill market-brief AAPL")
    original = service.execution.get(accepted.turn_id)
    ref = original["release_snapshot"]["requested_skills"][0]
    assert ref["skill_id"] == "market-brief" and len(ref["package_hash"]) == 64
    service.releases.skills = SkillCatalog()
    replay = admit(
        message="/skill market-brief AAPL",
        conversation_id=accepted.conversation_id,
        key=accepted.conversation_id,
    )
    assert replay.turn_id == accepted.turn_id
    assert (
        service.execution.get(accepted.turn_id)["release_snapshot"] == original["release_snapshot"]
    )
    journal = service.journal.messages_for_turn(accepted.conversation_id, accepted.turn_id)
    assert journal[0].content == "/skill market-brief AAPL"


def test_missing_choice_rejected_before_business_turn(service, admit):
    """S04：不合法前缀没有创建半个 Turn 或原生运行。"""
    with pytest.raises(SkillError) as error:
        admit(message="/skill")
    assert error.value.code == "SKILL_DIRECTIVE_INVALID"
    assert not service.journal.list_incomplete_turns()


def test_explicit_progress_comes_from_real_graph_and_has_no_tool_identity():
    """S24：显式准备原生 custom 流恰好 preparing/prepared，不伪造工具调用。"""
    graph, _ = setup([AIMessage(content="完成")])
    cfg = {"configurable": {"thread_id": "progress"}}
    events = list(
        graph.stream(
            {"messages": [HumanMessage(content="/skill market-brief AAPL", id="u")]},
            cfg,
            context=context("*"),
            stream_mode="custom",
        )
    )
    skill_events = [e for e in events if e.get("type") == "skill.preparation"]
    assert [e["status"] for e in skill_events] == ["preparing", "prepared"]
    assert len({e["event_id"] for e in skill_events}) == 1
    assert all(set(e) == {"type", "event_id", "skill", "source", "status"} for e in skill_events)
    assert not any(e.get("type") == "tool.progress" for e in events)
    assert not graph.get_state(cfg).values.get("run_tool_call_count", {}).get("__all__", 0)


@pytest.mark.asyncio
async def test_skill_preparation_replay_uses_existing_cursor_cas_and_card(runtime):
    """S24：只替换外部飞书传输，真实通知事务保留有界幂等准备状态。"""
    _, _, _, gateway, turn, command = await started(runtime)
    stream = TurnAnswerStream(runtime.turns, runtime.turns.lifecycle.native)
    state = stream.read(turn, command)
    for index, status in enumerate(["preparing", "failed", "preparing", "prepared"]):
        cursor = state.get("cursor")
        event = SkillPreparation(
            event_id="a" * 64, skill="market-brief", status=status
        ).model_dump()
        consume(state, StreamPart("custom", event, str(index)))
        assert stream.save(turn, command, state, expected_cursor=cursor)
    _, gateway = await publish(runtime, gateway)
    assert "技能准备就绪" in gateway.calls[-1]["content"]
    assert "a" * 64 not in gateway.calls[-1]["content"]
    assert not state.get("tools")
    consume(state, StreamPart("custom", {**event, "body": "PRIVATE"}, "5"))
    assert "PRIVATE" not in json.dumps(state)
    with runtime.turns.sessions() as session:
        before = session.scalar(select(func.count()).select_from(NotificationEventRow))
    assert stream.save(turn, command, state, expected_cursor="3")
    with runtime.turns.sessions() as session:
        assert session.scalar(select(func.count()).select_from(NotificationEventRow)) == before


@pytest.mark.asyncio
async def test_feishu_invalid_skill_is_a_business_error(runtime):
    """确定的输入错误返回可修正提示，渠道不将其视为暂态运行失败。"""
    service, gateway = channel(runtime), Replies()
    assert await service.process(message("/skill"), gateway) == "skill_rejected"
    assert gateway.texts == ["请使用 /skill 技能名称 任务正文。"]
    assert not runtime.client.calls


@pytest.mark.asyncio
async def test_explicit_prepare_failure_is_not_a_completed_business_answer(runtime):
    """初始化受控结束保留计量，但 BFF 不把没有模型回答的成功图状态报成完成。"""
    _, accepted, _, _, turn, command = await started(runtime)
    state = final_state(runtime, accepted)
    state["values"]["skill_state"] = {
        "turn_id": accepted.turn_id,
        "preparation_error": {
            "code": "SKILL_CONTEXT_BUDGET_EXCEEDED",
            "message": "当前任务内容无法容纳完整技能指导。",
        },
    }
    runtime.client.states[turn["thread_id"]] = state
    runtime.client.values[command["native_run_id"]]["status"] = "success"
    await tick(runtime)
    assert runtime.turns.execution.get(accepted.turn_id)["status"] == "failed"
