"""真实根图统一中断、增量回答与子任务上下文的完整闭环。"""

import json
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.graphs.ziwei_agent import build_ziwei_agent
from financeclaw.agent_server.tools.task_context import answered_clarifications
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.releases.interactions import CLARIFICATION_TOOL
from tests.stage6fix.test_batch_tools import call
from tests.stage7.support import request
from tests.stage8_hotfix.test_production_subgraphs import root_graph
from tests.stage8_hotfix.test_worker_policies import stack as stack


class ContinuingRoot(OfflineFinanceModel):
    """恢复后只派发未完成的 Worker，完整参数仍由子模型依据上下文填写。"""

    calls: list[dict]

    def _generate(self, messages, *args, **kwargs):
        """根只维护任务进度，不把原问题或回答重新提取成出生参数。"""
        answers = answered_clarifications(messages)
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        if not receipts:
            answer = AIMessage(content="", tool_calls=self.calls)
        elif answers and receipts[-1].name == CLARIFICATION_TOOL:
            pending = answers[-1]["requests"]
            answer = AIMessage(
                content="",
                tool_calls=[
                    call(
                        item["tool"],
                        f"retry-{len(answers)}-{index}",
                        task="继续未完成排盘",
                        arguments={},
                    )
                    for index, item in enumerate(pending)
                ],
            )
        else:
            answer = AIMessage(content="任务已完成，已复用成功结果。")
        return ChatResult(generations=[ChatGeneration(message=answer)])


class IncrementalZiwei(OfflineFinanceModel):
    """对合成 JSON 原文应用真实澄清短句，执行实际排盘工具。"""

    seen: ClassVar[list[dict]] = []

    def _generate(self, messages, *args, **kwargs):
        """不依赖主 Agent 重传完整参数；所有事实都从实际收到的上下文取得。"""
        if isinstance(messages[-1], ToolMessage):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="取证完成"))])
        value = json.loads(next(m.content for m in reversed(messages) if m.type == "human"))
        type(self).seen.append(value)
        arguments = json.loads(value["user_context"]["content"].split("\n")[-1])
        if value["arguments"].get("birth"):
            arguments = value["arguments"]  # 同批已有完整参数的独立任务
        else:
            for clarification in value["clarifications"]:
                text = clarification["answer"]["text"]
                if text == "公历":
                    assert "公历" in clarification["question"]
                    arguments["birth"]["calendar"] = "solar"
                elif text == "女性":
                    assert "性别" in clarification["question"]
                    arguments["birth"]["sex_for_chart"] = "female"
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "ziwei_chart",
                                "args": arguments,
                                "id": "chart-from-context",
                            }
                        ],
                    )
                )
            ]
        )


def install_child(stack):
    """保留发布与治理配置，只注入确定性的模型替身。"""
    IncrementalZiwei.seen = []
    tool = stack.tool_catalog.resolve("call_agent__ziwei_doushu_agent").tool
    tool.graph = build_ziwei_agent(
        stack.agent_factory, tool.release, stack.ziwei_service, model=IncrementalZiwei()
    )


def original(*, two_missing=False):
    """用户已提供所有资料，仅缺历法，部分测试再缺性别。"""
    data = request(
        level="yearly",
        question="查询今年流年",
        focus="wealth",
        target={"kind": "relative_period", "unit": "year", "offset": 0},
    ).model_dump(mode="json")
    data["birth"]["calendar"] = None
    if two_missing:
        data["birth"]["sex_for_chart"] = None
    return json.dumps(data, ensure_ascii=False)


def resume(wait, text):
    """只回复一个自然语言字段，不能通过测试替身暗中重传整份参数。"""
    return Command(resume={wait.id: {"kind": "input", "answer": {"text": text}}})


@pytest.mark.asyncio
@pytest.mark.parametrize("directive", [False, True])
async def test_one_field_answer_resumes_same_task_with_original_context(stack, directive):
    """子图下一次调用同时看到原请求、问题、回答与固定时间，不再重复问历法。"""
    calls = [call("call_agent__ziwei_doushu_agent", 1, task="排盘", arguments={})]
    graph, kwargs = root_graph(stack, calls, model=ContinuingRoot(calls=calls))
    install_child(stack)
    text = ("/agent ziwei_doushu_agent 请使用以下合成资料\n" if directive else "") + original()
    value = await graph.ainvoke({"messages": [HumanMessage(content=text, id="original")]}, **kwargs)
    wait = value["__interrupt__"][0]
    assert wait.value["point_id"] == "clarification" and "历" in wait.value["question"]
    before = stack.conversation_repository.execution.get("root")
    assert before["model_calls"] == 2  # 根派发一次、子模型一次，无模型参与汇总
    # 从保存的 checkpoint 重建根图，不能依赖旧工具实例内的“当前出生资料”。
    graph = stack.agent_factory.build(
        stack.agent_profiles.resolve("finance_agent", "1.5.0"),
        model=ContinuingRoot(calls=calls),
        checkpointer=graph.checkpointer,
        fallback_models=(),
    )
    result = await graph.ainvoke(resume(wait, "公历"), **kwargs)
    assert not result.get("__interrupt__")
    assert result["messages"][-1].content == "任务已完成，已复用成功结果。"
    assert len(IncrementalZiwei.seen) == 2
    second = IncrementalZiwei.seen[-1]
    assert second["user_context"] == {"message_id": "original", "content": text}
    assert second["arguments"] == {}  # 父模型没有重复提取完整参数
    assert second["clarifications"][0]["answer"] == {"text": "公历"}
    assert second["clarifications"][0]["requests"][0]["missing_fields"] == ["birth.calendar"]
    assert second["time_context"] == {
        "request_clock": kwargs["context"].request_clock,
        "timezone": kwargs["context"].timezone,
    }
    receipts = [
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name.startswith("call_agent")
    ]
    assert [json.loads(m.content)["outcome"] for m in receipts] == [
        "needs_clarification",
        "chart_only",
    ]
    target = json.loads(receipts[-1].content)["charts_used"][0]["target"]
    assert (target["start"], target["end"]) == ("2026-01-01", "2027-01-01")


@pytest.mark.asyncio
async def test_batch_waits_for_success_and_only_retries_unfinished_worker(stack):
    """汇合后只有一个根中断，成功 Worker 的回执和命盘不因恢复重新执行。"""
    calls = [
        call("call_agent__ziwei_doushu_agent", 1, task="需补历法", arguments={}),
        call(
            "call_agent__ziwei_doushu_agent",
            2,
            task="已有完整资料",
            arguments=request(level="natal", target=None).model_dump(mode="json"),
        ),
    ]
    graph, kwargs = root_graph(stack, calls, model=ContinuingRoot(calls=calls))
    install_child(stack)
    first = await graph.ainvoke({"messages": [HumanMessage(content=original())]}, **kwargs)
    assert len(first["__interrupt__"]) == 1
    receipts = [m for m in first["messages"] if isinstance(m, ToolMessage)]
    assert [json.loads(m.content)["outcome"] for m in receipts] == [
        "needs_clarification",
        "chart_only",
    ]
    successful = receipts[1]
    result = await graph.ainvoke(resume(first["__interrupt__"][0], "公历"), **kwargs)
    assert not result.get("__interrupt__")
    assert len(IncrementalZiwei.seen) == 3
    assert result["messages"].count(successful) == 1
    assert sum(v["task"] == "已有完整资料" for v in IncrementalZiwei.seen) == 1


@pytest.mark.asyncio
async def test_multiple_answers_accumulate_without_losing_previous_reply(stack):
    """最初一次问两个缺项；用户只答一个时，下一次只询问仍未知的一项。"""
    calls = [call("call_agent__ziwei_doushu_agent", 1, task="排盘", arguments={})]
    graph, kwargs = root_graph(stack, calls, model=ContinuingRoot(calls=calls))
    install_child(stack)
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content=original(two_missing=True))]}, **kwargs
    )
    second = await graph.ainvoke(resume(first["__interrupt__"][0], "公历"), **kwargs)
    wait = second["__interrupt__"][0]
    assert "性别" in wait.value["question"] and "公历" not in wait.value["question"]
    assert wait.id != first["__interrupt__"][0].id
    result = await graph.ainvoke(resume(wait, "女性"), **kwargs)
    assert not result.get("__interrupt__")
    assert [v["answer"]["text"] for v in IncrementalZiwei.seen[-1]["clarifications"]] == [
        "公历",
        "女性",
    ]


@pytest.mark.asyncio
async def test_cancel_during_root_clarification_blocks_resume(stack):
    """澄清恢复仍受同一根任务的持久授权与取消检查保护。"""
    calls = [call("call_agent__ziwei_doushu_agent", 1, task="排盘", arguments={})]
    graph, kwargs = root_graph(stack, calls, model=ContinuingRoot(calls=calls))
    install_child(stack)
    first = await graph.ainvoke({"messages": [HumanMessage(content=original())]}, **kwargs)
    stack.conversation_repository.execution.request_cancel("root")
    with pytest.raises(ExecutionConflict, match="cancel"):
        await graph.ainvoke(resume(first["__interrupt__"][0], "公历"), **kwargs)
    assert len(IncrementalZiwei.seen) == 1
