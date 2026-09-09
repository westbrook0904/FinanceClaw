"""按根任务原始消息和已完成交互组织上下文，不调用模型抽取资料。"""

import json

from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.releases.interactions import CLARIFICATION_TOOL, ROOT_CLARIFICATION


def answered_clarifications(messages):
    """仅采信配对成功的根澄清 Tool 回执；保留问题和回答以解释简短补充。"""
    pending, answers = {}, []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["name"] == CLARIFICATION_TOOL:
                    pending[call["id"]] = (
                        call,
                        message.additional_kwargs.get("clarification_requests", []),
                    )
        elif isinstance(message, ToolMessage) and message.tool_call_id in pending:
            call, requests = pending.pop(message.tool_call_id)
            if message.status != "success" or message.name != CLARIFICATION_TOOL:
                continue
            try:
                value = json.loads(message.content)
            except (ValueError, TypeError):
                raise ExecutionConflict("invalid clarification receipt") from None
            if (
                not isinstance(value, dict)
                or value.get("kind") != "input"
                or not Draft202012Validator(ROOT_CLARIFICATION.response_schema).is_valid(
                    value.get("answer")
                )
            ):
                raise ExecutionConflict("clarification answer violates its release")
            answers.append(
                {
                    "tool_call_id": message.tool_call_id,
                    "question": call["args"]["question"],
                    "answer": value["answer"],
                    "requests": requests,
                }
            )
    return answers


def task_context(runtime, snapshot):
    """使用 BFF 固定的本轮消息锚点；恢复不会改成最后一条回答或混入旧任务。"""
    messages = runtime.state.get("messages", [])
    origin = snapshot.get("user_message_id")
    indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage) and (origin is None or message.id == origin)
    ]
    if not indices or (origin is not None and len(indices) != 1):
        raise ExecutionConflict("root task has no unique original user message")
    # 非 BFF 的原生图测试没有消息锚点，使用最后一条用户消息作为本次任务入口。
    start = indices[-1]
    original = messages[start]
    context = ExecutionContext.model_validate(runtime.context)
    return {
        "user_context": {"message_id": original.id, "content": original.content},
        "clarifications": answered_clarifications(messages[start:]),
        "time_context": {"request_clock": context.request_clock, "timezone": context.timezone},
    }
