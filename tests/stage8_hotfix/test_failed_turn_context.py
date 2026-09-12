"""飞书失败后通过新线程把原问题、补充回答和结果状态送到下一次模型请求。"""

from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select

from financeclaw.agent_server.context.compaction import NativeContextMiddleware
from financeclaw.agent_server.context.planning import completed_tool_batches
from financeclaw.shared.llm.budget import ContextBudget
from financeclaw.shared.turns.budget import snapshot_context
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from tests.stage8_hotfix.test_feishu_clarification import Replies, ask, channel, message
from tests.stage10.runtime import final_state, tick
from tests.stage10.runtime import runtime as runtime


def latest(runtime):
    """仅测试内定位最新受理轮次，生产继续以当前命令和用户消息锚点为准。"""
    with runtime.turns.sessions() as session:
        row = session.scalar(
            select(ConversationTurnRow).order_by(ConversationTurnRow.created_at.desc())
        )
        command = session.get(TurnCommandRow, row.current_command_id)
        return SimpleNamespace(
            turn_id=row.turn_id,
            thread_id=row.thread_id,
            status=row.status,
            input=command.request_payload.get("input"),
            native_id=command.native_run_id,
            context=snapshot_context(row.release_snapshot, command_id=command.command_id),
        )


class HistoryCheckingModel(FakeMessagesListChatModel):
    """在真实模型调用入口检查历史，使仅查询仓储但未送入模型的修复不能通过。"""

    followup: str

    def _generate(self, messages, *args, **kwargs):
        """失败记录含已确认补充，无悬空工具链，当前提问保持原文。"""
        contents = [m.content for m in messages]
        assert contents[0:3] == ["旧成功问题", "旧问题的结果", "失败的问题"]
        assert "本轮处理失败" in contents[3]
        assert "当地钟表时间" in contents[3]
        assert "未获得有效的最终答案" in contents[3]
        assert contents[-1] == self.followup
        assert completed_tool_batches(messages) == []
        return super()._generate(messages, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", ["重试", "刚才失败的问题，换个角度再分析"])
async def test_failed_turn_and_answer_reach_next_model_for_any_followup(runtime, followup):
    """成功、失败、追问的真实受理链路，不靠特判重试文字来恢复目标。"""
    service = channel(runtime)
    await service.process(message("旧成功问题", identifier="old"), Replies())
    await tick(runtime)
    final_state(runtime, latest(runtime), content="旧问题的结果")
    await tick(runtime)
    await service.process(message("失败的问题", identifier="failed"), Replies())
    await tick(runtime)
    failed = latest(runtime)
    await ask(runtime, failed)
    await service.process(message("当地钟表时间", identifier="answer"), Replies())
    await tick(runtime)
    runtime.client.values[latest(runtime).native_id]["status"] = "error"
    await tick(runtime)
    assert latest(runtime).status == "failed"

    await service.process(message(followup, identifier="followup"), Replies())
    await tick(runtime)
    current = latest(runtime)
    assert current.thread_id != failed.thread_id
    middleware = NativeContextMiddleware(
        ContextBudget(
            model_input_limit=20000,
            reserved_output_tokens=512,
            system_policy_reserve=0,
            tool_schema_reserve=0,
            safety_margin=128,
            recent_turns=4,
        ),
        runtime.turns.journal,
    )
    graph = create_agent(
        HistoryCheckingModel(followup=followup, responses=[AIMessage(content="处理失败的问题")]),
        middleware=[middleware],
    )
    result = await graph.ainvoke(current.input, context=current.context)
    assert result["messages"][-1].content == "处理失败的问题"
