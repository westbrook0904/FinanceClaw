"""真实模型转换包含 invalid_tool_calls，回执必须覆盖整个批次。"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai.chat_models.base import _convert_message_to_dict

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.context.planning import completed_tool_batches
from financeclaw.shared.turns.types import ExecutionConflict
from tests.stage1.test_agent import components_with_tools, context


class MalformedBatchModel(OfflineFinanceModel):
    """首轮混合合法与损坏 JSON，后续按 Provider 的实际转换核对完整工具回执。"""

    valid: bool = True
    invalid_id: str | None = "bad-call"

    def _generate(self, messages, *args, **kwargs):
        """把缺回执当成远端 400，保证合成模型不会掩盖生产协议问题。"""
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        if receipts:
            original = next(m for m in messages if isinstance(m, AIMessage))
            wire = _convert_message_to_dict(original)
            assert {c["id"] for c in wire["tool_calls"]} == {m.tool_call_id for m in receipts}
            assert all(m.status == "error" for m in receipts)
            assert completed_tool_batches(messages) is not None
            answer = AIMessage(content="已收到整批格式错误，可以重新填写参数。")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "calculate",
                        "id": "valid-call",
                        "args": {"operation": "add", "left": 1, "right": 2},
                    }
                ]
                if self.valid
                else [],
                invalid_tool_calls=[
                    {
                        "name": "calculate",
                        "id": self.invalid_id,
                        "args": '{"operation":',
                        "error": "invalid JSON",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("valid", [False, True])
async def test_invalid_calls_receive_error_before_next_real_model_request(asynchronous, valid):
    """纯无效批次和混合批次均不执行工具，每个 Provider 调用 ID 都有明确回执。"""
    stack, _ = components_with_tools()
    graph = stack.agent_factory.build(
        stack.default_agent_profile, model=MalformedBatchModel(valid=valid)
    )
    inputs = {"messages": [{"role": "user", "content": "计算"}]}
    options = {
        "context": context("tools:read"),
        "config": {"configurable": {"thread_id": "invalid-tools"}},
    }
    result = (
        await graph.ainvoke(inputs, **options) if asynchronous else graph.invoke(inputs, **options)
    )
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage)]) == 1 + valid
    assert result["messages"][-1].content.startswith("已收到整批格式错误")


@pytest.mark.parametrize("identifier", [None, "valid-call"])
def test_ambiguous_invalid_call_identity_stops_before_dispatch(identifier):
    """缺失或重复 ID 不能伪造回执，也不能执行同批次的有效工具。"""
    stack, _ = components_with_tools()
    graph = stack.agent_factory.build(
        stack.default_agent_profile, model=MalformedBatchModel(invalid_id=identifier)
    )
    with pytest.raises(ExecutionConflict, match="missing or duplicate"):
        graph.invoke(
            {"messages": [{"role": "user", "content": "计算"}]},
            context=context("tools:read"),
            config={"configurable": {"thread_id": "bad-identity"}},
        )


def test_compaction_rejects_missing_invalid_call_receipt():
    """即使所有可执行调用已返回，也不能把缺少无效调用回执的批次当成完整记录。"""
    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "calculate",
                "id": "bad",
                "args": "{",
                "error": "invalid JSON",
            }
        ],
    )
    assert completed_tool_batches([message]) is None
    assert completed_tool_batches(
        [message, ToolMessage(content="invalid", tool_call_id="bad")]
    ) == [(0, 1)]
