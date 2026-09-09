"""原生最终文本提取与 BFF 持久进度的安全事件投影。"""

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage

from financeclaw.kernel.responses import StreamEvent


def completed_stream_event(run_id: str, output: Mapping[str, Any] | None) -> StreamEvent:
    """按最终输出构造 ``assistant.completed`` 事件。

    Args:
        run_id: FinanceClaw 业务运行 ID。
        output: Agent Server 或业务仓储中的最终输出。

    Returns:
        仅含 run ID 与完整助手文本（若存在）的稳定终态事件。

    """
    content = final_assistant_content(output or {})
    data: dict[str, Any] = {"run_id": run_id}
    if content is not None:
        data["content"] = content
    return StreamEvent(event="assistant.completed", data=data)


def interrupted_stream_event(
    run_id: str,
    *,
    waiting_reason: str | None = None,
    pending_interactions: tuple[dict[str, Any], ...] = (),
) -> StreamEvent:
    """只投影应用层已脱敏的等待原因和审批对象，不透传图内部 state。"""
    data: dict[str, Any] = {"run_id": run_id}
    if waiting_reason is not None:
        data["waiting_reason"] = waiting_reason
    if pending_interactions:
        data["pending_interactions"] = list(pending_interactions)
    return StreamEvent(event="run.interrupted", data=data)


def failed_stream_event(run_id: str) -> StreamEvent:
    """构造不暴露异常与内部状态的 ``run.failed`` 事件。"""
    return StreamEvent(event="run.failed", data={"run_id": run_id})


def final_assistant_content(output: Mapping[str, Any]) -> str | None:
    """从最终状态的消息列表中提取最后一条助手文本。

    Args:
        output: 最终运行输出，通常包含 ``messages``。

    Returns:
        最后一条 AI/assistant 消息的纯文本表示；不存在时返回 ``None``。

    """
    messages = output.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return _content_text(message.content)
        if isinstance(message, Mapping) and _is_assistant_message(message):
            return _content_text(message.get("content"))
    return None


def _is_assistant_message(message: Mapping[str, Any]) -> bool:
    """判断序列化消息是否属于助手，而不是用户或工具。"""
    kind = str(message.get("type", message.get("role", ""))).lower()
    return kind in {
        "ai",
        "assistant",
        "aimessage",
        "aimessagechunk",
        "ai_message",
        "ai_message_chunk",
    }


def _content_text(content: Any) -> str | None:
    """把字符串或文本内容块序列规范化为可展示文本。"""
    if isinstance(content, str):
        return content or None
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}:
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces) or None
    if content is None:
        return None
    return None
