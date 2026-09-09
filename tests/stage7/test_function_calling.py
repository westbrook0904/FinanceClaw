"""通过完整 Tool Schema 填参，工具内聚合校验，不增加提取或预检模型阶段。"""

import json
from threading import Barrier

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.ziwei_offline import OfflineZiweiModel
from tests.stage7.support import build_ziwei_agent, components, context, request


class PlannedZiweiModel(OfflineZiweiModel):
    """发送指定的真实 function calls，成功后结束取证；不模拟领域计算。"""

    batches: list[list[dict]]

    def _generate(self, messages, *args, **kwargs):
        """按已有 Tool 回执决定下一批参数，也能验证一次格式修复。"""
        count = sum(isinstance(m, AIMessage) and bool(m.tool_calls) for m in messages)
        if count >= len(self.batches):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="取证完成"))])
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "ziwei_chart", "args": value, "id": f"chart-{count}-{index}"}
                            for index, value in enumerate(self.batches[count])
                        ],
                    )
                )
            ]
        )


async def invoke(stack, batches):
    """参数完全由 function call 提供，入口只包含自然语言任务和原始提示。"""
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=PlannedZiweiModel(batches=batches),
    )
    return await graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "读取上下文排盘",
                            "arguments": {"birth": "出生资料在授权引用中"},
                        }
                    ),
                }
            ]
        },
        context=context(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, expected",
    [
        (
            {"level": "yearly"},
            {
                "birth.calendar",
                "birth.date",
                "birth.time",
                "birth.time_basis",
                "birth.place",
                "birth.sex_for_chart",
                "target",
            },
        ),
        (
            {
                "birth": {"date": {"year": 2000}},
                "level": "monthly",
                "target": {"kind": "calendar_period", "unit": "month"},
            },
            {
                "birth.date.month",
                "birth.date.day",
                "birth.calendar",
                "birth.time",
                "birth.place",
                "birth.time_basis",
                "birth.sex_for_chart",
                "target.year",
                "target.month",
            },
        ),
        (
            {
                "birth": {
                    "calendar": "solar",
                    "date": {"year": 2000, "month": 2, "day": 30},
                    "time": {"kind": "shichen", "shichen": "zi"},
                    "time_basis": "civil",
                    "place": {"name": "未知地点"},
                    "sex_for_chart": "female",
                },
                "level": "daily",
                "target": {"kind": "bounded_range"},
            },
            {
                "birth.date",
                "birth.time",
                "birth.place.timezone",
                "target.start",
                "target.end",
            },
        ),
    ],
)
async def test_all_identifiable_issues_return_after_one_call(arguments, expected):
    """缺资料、无效日期、时辰歧义与目标缺失一起返回，模型无法再猜值重试。"""
    result = await invoke(components(), [[arguments], [request().model_dump(mode="json")]])
    public = result["ziwei_result"]
    assert public["outcome"] == "needs_clarification"
    assert set(public["missing_fields"]) == expected
    assert {issue["field"] for issue in public["issues"]} == expected
    assert result["ziwei_model_calls"] == 1
    assert not result.get("ziwei_evidence")
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage)]) == 1


@pytest.mark.asyncio
async def test_one_structural_repair_can_use_existing_context():
    """格式错误允许一次修复，修复成功后返回实际计算结果。"""
    valid = request(level="natal", target=None).model_dump(mode="json")
    result = await invoke(components(), [[{**valid, "focus": "invalid-format"}], [valid]])
    assert result["ziwei_result"]["outcome"] == "chart_only"
    assert result["ziwei_input_repairs"] == 1
    assert result["ziwei_model_calls"] == 3  # 两次 function call + 一次结束取证
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.status for m in receipts] == ["error", "success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_chart", [False, True])
async def test_one_child_can_concurrently_call_different_chart_queries(monkeypatch, same_chart):
    """同一 child 内两次调用并发落到引擎，每份参数和投影都保留独立绑定。"""
    stack = components()
    barrier = Barrier(2, timeout=5)
    calculate = stack.ziwei_service.calculate

    def concurrent(*args):
        """串行执行会超时，不能把普通的顺序调用当作并发通过。"""
        barrier.wait()
        return calculate(*args)

    monkeypatch.setattr(stack.ziwei_service, "calculate", concurrent)
    first = request(level="natal", target=None, focus="career").model_dump(mode="json")
    second = request(
        level="natal" if same_chart else "yearly",
        target=None if same_chart else {"kind": "point", "on_date": "2026-09-09"},
        focus="wealth",
    ).model_dump(mode="json")
    result = await invoke(stack, [[first, second]])
    public = result["ziwei_result"]
    assert public["outcome"] == "chart_only"
    evidence = result["ziwei_evidence"]
    assert len(evidence) == 2
    assert {item["tool_call_id"] for item in evidence} == {"chart-0-0", "chart-0-1"}
    assert {item["request"]["focus"] for item in evidence} == {"career", "wealth"}
    assert len(public["charts_used"]) == 2
    ids = {item["chart_id"] for item in public["charts_used"]}
    assert len(ids) == (1 if same_chart else 2)


@pytest.mark.asyncio
async def test_schema_and_missing_errors_are_both_reported():
    """存在缺失字段时，不丢弃同次发现的其他格式错误。"""
    result = await invoke(components(), [[{"level": "yearly", "focus": "invalid-format"}]])
    public = result["ziwei_result"]
    assert public["outcome"] == "needs_clarification"
    assert {"focus", "birth.date", "target"} <= {item["field"] for item in public["issues"]}
    assert "invalid-format" not in json.dumps(public)
