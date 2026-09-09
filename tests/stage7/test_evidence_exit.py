"""取证阶段失败必须交回根会话，不能依赖模型自行停止重试。"""

import json
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.kernel.ziwei import ZiweiTextResult
from tests.stage7.support import build_ziwei_agent, components, context, envelope, request


class RepeatingEvidenceModel(OfflineZiweiModel):
    """始终重试相同工具；由图的退出路由而非合作式模型保证终止。"""

    invalid_arguments: bool = False
    batch_size: int = 1
    calls: ClassVar[list[str]] = []

    def _generate(self, messages, *args, **kwargs):
        """记录真实模型调用，包括任何不应发生的 finalize。"""
        type(self).calls.append("evidence" if self._bound_tool_names else "finalize")
        task = json.loads(
            next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
        )
        arguments = task["arguments"]
        if self.invalid_arguments:
            arguments["focus"] = "invalid-focus"
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "ziwei_chart",
                                "args": arguments,
                                "id": f"chart-{len(type(self).calls)}-{index}",
                            }
                            for index in range(self.batch_size)
                        ],
                    )
                )
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chart_only", "interpretation"])
@pytest.mark.parametrize("failure", ["missing_input", "invalid_tool_arguments"])
async def test_evidence_failure_exits_without_retry_or_finalization(monkeypatch, mode, failure):
    """缺失事实立即终止；已有资料的格式问题最多修复一次。"""
    stack = components()
    RepeatingEvidenceModel.calls = []
    if failure == "missing_input":

        def missing(*args, **kwargs):
            """模拟只有计算工具才发现的必填资料缺失。"""
            raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请补充查询日期。", ("target",))

        monkeypatch.setattr(stack.ziwei_service, "calculate", missing)
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=RepeatingEvidenceModel(invalid_arguments=failure == "invalid_tool_arguments"),
    )
    result = await graph.ainvoke(envelope(request(mode=mode)), context=context())
    public = ZiweiTextResult.model_validate(result["ziwei_result"])
    assert public.outcome == (
        "needs_clarification" if failure == "missing_input" else "unsupported"
    )
    if failure == "missing_input":
        assert public.missing_fields == ("target",)
        assert public.question == "请补充查询日期。"
        assert public.error_code == "ZIWEI_INPUT_INCOMPLETE"
    else:
        assert public.error_code == "ZIWEI_TOOL_INPUT_INVALID"
        assert "invalid-focus" not in public.model_dump_json()
    expected = 1 if failure == "missing_input" else 2
    assert RepeatingEvidenceModel.calls == ["evidence"] * expected
    assert result["ziwei_model_calls"] == expected
    assert not public.charts_used and not public.answer_text
    assert not result.get("__interrupt__")
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(receipts) == expected and all(m.status == "error" for m in receipts)
    assert receipts[0].tool_call_id == "chart-1-0"


@pytest.mark.asyncio
async def test_parallel_evidence_failures_have_one_terminal_result(monkeypatch):
    """并发 Tool 错误补齐所有回执，结束节点只写一次领域终态。"""
    stack = components()
    RepeatingEvidenceModel.calls = []

    def missing(*args, **kwargs):
        """同一批的工具报告相同缺失字段。"""
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请补充查询日期。", ("target",))

    monkeypatch.setattr(stack.ziwei_service, "calculate", missing)
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=RepeatingEvidenceModel(batch_size=2),
    )
    result = await graph.ainvoke(envelope(request()), context=context())
    assert result["ziwei_result"]["outcome"] == "needs_clarification"
    receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in receipts} == {"chart-1-0", "chart-1-1"}
    assert all(m.status == "error" for m in receipts)
    assert RepeatingEvidenceModel.calls == ["evidence"]


@pytest.mark.asyncio
async def test_evidence_does_not_convert_permission_failure_to_clarification(monkeypatch):
    """真正的权限异常仍受控失败，不包装成可让用户补参数重试的结果。"""
    stack = components()

    def denied(*args, **kwargs):
        """模拟工具层再次鉴权拒绝。"""
        raise PermissionError("ziwei:read is required")

    monkeypatch.setattr(stack.ziwei_service, "calculate", denied)
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=RepeatingEvidenceModel(),
    )
    with pytest.raises(PermissionError, match="ziwei:read"):
        await graph.ainvoke(envelope(request()), context=context())
