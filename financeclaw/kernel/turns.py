"""真实用户 Turn 的消息边界；同时适配原生消息对象与 SDK 字典。"""

from collections.abc import Mapping, Sequence
from typing import Any


def message_value(message: Any, key: str, default: Any = None) -> Any:
    """读取消息公共字段，不让共享模块依赖 LangChain。"""
    return (
        message.get(key, default)
        if isinstance(message, Mapping)
        else getattr(message, key, default)
    )


def is_user_message(message: Any) -> bool:
    """排除原生摘要等合成 HumanMessage，保留真实用户来源。"""
    role = message_value(message, "type", message_value(message, "role"))
    metadata = message_value(message, "additional_kwargs", {}) or {}
    return role in {"human", "user"} and metadata.get("lc_source") != "summarization"


def current_turn_start(messages: Sequence[Any], user_message_id: str | None) -> int:
    """按冻结的用户消息 ID 定位；无冻结 ID 的独立运行取最后一条真实用户消息。"""
    matches = [
        index
        for index, message in enumerate(messages)
        if is_user_message(message)
        and (user_message_id is None or message_value(message, "id") == user_message_id)
    ]
    if not matches or (user_message_id is not None and len(matches) != 1):
        raise ValueError("native state has no unique current Turn input")
    return matches[-1]


def message_source(context: Any, *, user_message_id: str | None = None) -> dict[str, Any]:
    """由受信任执行上下文生成归档与 Turn 边界信息。"""
    return {
        "conversation_id": context.conversation_id,
        "turn_id": context.turn_id,
        "run_id": context.run_id,
        "user_message_id": user_message_id,
    }
