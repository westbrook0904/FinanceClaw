"""飞书明确命令协议：从已验证文本中取得交互身份，不从自然语言推测授权。"""

import json
from typing import Any

from financeclaw.coordination.api import InteractionConflict
from financeclaw.kernel.interactions import InteractionResponse


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
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...], *, fallback: str
) -> str:
    """生成可复制命令和摘要；旧消息重发的决定仍由服务端校验生命周期。"""
    if len(items) != 1 or "interaction_id" not in items[0]:
        return fallback
    item = items[0]
    identifier, revision = item["interaction_id"], item["revision"]
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
            lines.append(f'/answer {identifier} {revision} {{"按上述格式填写": "回答"}}')
    lines.extend([f"/cancel {item['root_run_id']}", f"API：{item['response_url']}"])
    return "\n\n".join(lines)
