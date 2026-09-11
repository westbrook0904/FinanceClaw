"""飞书交互展示：文本澄清直接回答，其他交互使用明确命令。"""

import json
from typing import Any

from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.shared.turns.types import InteractionConflict


def accepts_text_reply(item: dict[str, Any]) -> bool:
    """只有单个 text 字段的资料交互可映射普通回复；审批和选择仍显式绑定。"""
    schema = item.get("response_schema", {})
    properties = schema.get("properties", {})
    return (
        item.get("kind") == "input"
        and schema.get("type") == "object"
        and set(properties) == {"text"}
        and properties["text"].get("type") == "string"
        and schema.get("required") == ["text"]
    )


def parse_response(text: str) -> tuple[str, InteractionResponse] | None:
    """命令含实例与版本；审批额外要求动作摘要，资料回答使用 JSON。"""
    parts = text.split(maxsplit=3)
    if not parts or parts[0] not in {"/answer", "/choose", "/approve", "/reject"}:
        return None
    if len(parts) != 4:
        raise InteractionConflict("交互命令需要交互 ID、版本和回答或动作摘要。")
    command, identifier, revision, raw = parts
    try:
        if command in {"/approve", "/reject"}:
            tokens = raw.split(maxsplit=1)
            response = InteractionResponse(
                revision=int(revision),
                kind="approval",
                decision="approve" if command == "/approve" else "reject",
                action_hash=tokens[0],
                reason=tokens[1] if len(tokens) == 2 else None,
            )
        else:
            response = InteractionResponse(
                revision=int(revision),
                kind="input" if command == "/answer" else "choice",
                answer=json.loads(raw),
            )
    except (ValueError, TypeError) as exc:
        raise InteractionConflict(
            "命令格式不正确；请使用显示的版本、摘要及 JSON 回答格式。"
        ) from exc
    return identifier, response


def format_interactions(
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    fallback: str,
    allow_text_reply: bool = True,
) -> str:
    """单一文本澄清只展示问题；有歧义时逐项提供明确的回答入口。"""
    if len(items) > 1:
        return "有几项内容需要分别确认，请使用各项下方的回答命令：\n\n" + "\n\n".join(
            format_interactions([item], fallback=fallback, allow_text_reply=False) for item in items
        )
    if len(items) != 1 or "interaction_id" not in items[0]:
        return fallback
    item = items[0]
    identifier, revision = item["interaction_id"], item["revision"]
    if accepts_text_reply(item) and allow_text_reply:
        if item["status"] == "pending":
            return item["question"] + "\n\n直接回复即可，我会接着处理。"
        if item["status"] == "expired":
            return (
                "这次提问已过期。请发送以下命令结束当前任务，再重新发起请求："
                f"\n/cancel {item['turn_id']}"
            )
        return "这次提问已处理或关闭，请以最新消息为准。"
    lines = [
        item["question"],
        f"交互：{identifier} · 版本：{revision}",
        f"截止：{item['expires_at']}",
    ]
    if item["status"] != "pending":
        lines.append(f"当前状态：{item['status']}。该问题已不可重新作决定，请查看任务状态。")
        return "\n\n".join(lines)
    if item["kind"] == "approval":
        action = json.dumps(item.get("action", {}), ensure_ascii=False)
        if len(action) > 4000:
            lines.append("动作较长，本消息未完整展示，因此不提供审批命令。")
            lines.append(f"请先通过已认证 API 查看完整动作：GET /v1/interactions/{identifier}")
        else:
            lines.append("动作：" + action)
            for decision in item["allowed_decisions"]:
                lines.append(f"/{decision} {identifier} {revision} {item['action_hash']}")
            lines.append("请核对动作后复制对应命令。普通文字“同意”不作为批准。")
    elif item["kind"] == "choice":
        if item["selection_mode"] == "multiple":
            lines.append(
                f"请选择 {item['min_selected']}—{item['max_selected']} 项："
                + "、".join(item["options"])
            )
            lines.append(
                f"/choose {identifier} {revision} "
                + json.dumps(item["options"][: item["min_selected"]], ensure_ascii=False)
            )
        else:
            lines.extend(
                f"/choose {identifier} {revision} {json.dumps(option, ensure_ascii=False)}"
                for option in item["options"]
            )
    else:
        schema = json.dumps(item["response_schema"], ensure_ascii=False)
        if len(schema) > 4000:
            lines.append(f"回答格式较长，请通过已认证 API 查看：GET /v1/interactions/{identifier}")
        else:
            lines.append("回答格式：" + schema)
            if accepts_text_reply(item):
                lines.append(f'/answer {identifier} {revision} {{"text": "你的回答"}}')
            else:
                lines.append(
                    f"请按上述字段填写 JSON，并发送：/answer {identifier} {revision} <JSON>"
                )
    lines.extend([f"/cancel {item['turn_id']}", f"API：{item['response_url']}"])
    return "\n\n".join(lines)
