"""当前任务上下文边界与工具公开 Schema；基础安装也必须执行。"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from financeclaw.agent_server.graphs.ziwei_agent import ZiweiEvidenceMiddleware
from financeclaw.agent_server.middleware.middleware import ToolGovernanceMiddleware
from financeclaw.agent_server.tools.task_context import answered_clarifications, task_context
from financeclaw.agent_server.tools.ziwei import ziwei_tools
from financeclaw.kernel.ziwei_tools import ZIWEI_TOOL_INPUTS
from financeclaw.shared.releases.interactions import CLARIFICATION_TOOL
from financeclaw.shared.turns.types import ExecutionConflict
from tests.stage7.support import context


def clarification_pair(answer):
    """固定工具调用与回执对应关系。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": CLARIFICATION_TOOL,
                    "args": {"question": "公历还是农历？"},
                    "id": "question",
                }
            ],
        ),
        ToolMessage(name=CLARIFICATION_TOOL, tool_call_id="question", content=json.dumps(answer)),
    ]


def test_current_turn_anchor_excludes_previous_task_answers():
    """同一聊天中的另一个任务不能继承旧问题的回答。"""
    messages = [
        HumanMessage(id="old", content="旧任务"),
        *clarification_pair({"kind": "input", "answer": {"text": "公历"}}),
        HumanMessage(id="new", content="新任务"),
    ]
    value = task_context(
        SimpleNamespace(state={"messages": messages}, context=context()), {"user_message_id": "new"}
    )
    assert value["clarifications"] == []
    assert value["user_context"] == {"message_id": "new", "content": "新任务"}


@pytest.mark.parametrize(
    "answer",
    [
        [],
        {"kind": "approval", "answer": {"text": "公历"}},
        {"kind": "input", "answer": {"calendar": "solar"}},
    ],
)
def test_invalid_clarification_receipt_is_not_treated_as_user_facts(answer):
    """只有固定交互契约的成功回答可以进入子 Agent 上下文。"""
    with pytest.raises(ExecutionConflict):
        answered_clarifications(clarification_pair(answer))


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_turn_governance_denial_into_clarification():
    """同一调用同时缺资料和未授权时，保留治理拒绝，不要求用户补参重试。"""
    call = SimpleNamespace(
        tool_call={"name": "ziwei_yearly_chart", "id": "denied", "args": {"birth": 1}}
    )
    denied = ToolGovernanceMiddleware._denied_message(call, "tool is not allowed")
    middleware = ZiweiEvidenceMiddleware(max_calls=4, input_budget=28000)
    assert middleware.wrap_tool_call(call, lambda _: denied) is denied

    async def handler(_):
        """模拟异步治理链返回已审计的拒绝回执。"""
        return denied

    assert await middleware.awrap_tool_call(call, handler) is denied


@pytest.mark.parametrize("index", range(5))
def test_ziwei_public_schema_has_fixed_fields_descriptions_and_relative_examples(index):
    """检查实际提供给模型的 Schema，而不仅是 Python 类型或源码注释。"""
    tool = ziwei_tools(None)[index].tool
    assert issubclass(tool.args_schema, ZIWEI_TOOL_INPUTS[tool.name])
    assert {"birth", "runtime"} <= set(tool.get_input_schema().model_fields)
    schema = convert_to_openai_tool(tool)["function"]["parameters"]
    assert not {"runtime", "target", "level"} & set(schema["properties"])

    def check_fields(value):
        """每一层业务字段都必须有模型可见的说明。"""
        if isinstance(value, dict):
            for name, field in value.get("properties", {}).items():
                assert field.get("description"), name
            for child in value.values():
                check_fields(child)
        elif isinstance(value, list):
            for child in value:
                check_fields(child)

    check_fields(schema)
    if index == 0:
        assert not {"on_date", "date_range", "year", "month"} & set(schema["properties"])
    else:
        offset = next(
            field for name, field in schema["properties"].items() if name.endswith("_offset")
        )
        assert 0 in offset["examples"]
        assert "request_clock" in offset["description"]
