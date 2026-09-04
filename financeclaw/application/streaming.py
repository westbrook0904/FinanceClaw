"""Agent Server 流事件投影：把供应商事件收敛为稳定且脱敏的应用事件。"""

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage

from financeclaw.kernel import StreamEvent


def project_server_part(part: Any) -> StreamEvent | None:
    """把单个 LangGraph 流片段投影为非终态稳定事件。

    仅公开助手文本增量与笼统运行进度，不透传节点更新、工具参数、Prompt
    或内部状态。终态由各应用服务在查询权威运行状态后另行生成。

    Args:
        part: SDK ``StreamPart``、映射或测试替身。

    Returns:
        ``assistant.delta``、``run.progress``，或无需公开时返回 ``None``。

    """
    event, data = _part_event_and_data(part)
    if event in {"messages", "messages/partial", "messages-tuple"}:
        delta = _assistant_delta(data)
        if delta:
            return StreamEvent(event="assistant.delta", data={"delta": delta})
        return None
    if event in {"updates", "values", "tasks", "metadata"}:
        return StreamEvent(event="run.progress", data={"status": "running"})
    return None


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


def interrupted_stream_event(run_id: str) -> StreamEvent:
    """构造不暴露中断内部载荷的 ``run.interrupted`` 事件。"""
    return StreamEvent(event="run.interrupted", data={"run_id": run_id})


def failed_stream_event(run_id: str) -> StreamEvent:
    """构造不暴露异常与内部状态的 ``run.failed`` 事件。"""
    return StreamEvent(event="run.failed", data={"run_id": run_id})


def progress_stream_event(run_id: str, status: str) -> StreamEvent:
    """构造仅携带公开状态的 ``run.progress`` 事件。"""
    return StreamEvent(event="run.progress", data={"run_id": run_id, "status": status})


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


def _part_event_and_data(part: Any) -> tuple[str, Any]:
    """从映射或 SDK 对象中读取事件名与数据。"""
    if isinstance(part, Mapping):
        return str(part.get("event", part.get("type", ""))), part.get("data")
    return str(getattr(part, "event", getattr(part, "type", ""))), getattr(part, "data", None)


def _assistant_delta(data: Any) -> str | None:
    """从 messages 模式的多种载荷形态中提取助手增量。"""
    candidate = data
    if isinstance(data, Sequence) and not isinstance(data, str | bytes):
        if not data:
            return None
        candidate = data[0]
    if not isinstance(candidate, Mapping) or not _is_assistant_message(candidate):
        return None
    return _content_text(candidate.get("content"))


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
