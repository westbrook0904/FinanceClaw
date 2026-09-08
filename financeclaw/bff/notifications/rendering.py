"""可版本化的纯文本通知；分片、内容与 UUID 在首次消费时固定。"""

from financeclaw.bff.application.feishu_interactions import format_interactions


def render(kind, payload) -> str:
    """正文只来自受理目标对应的根 Journal 或安全交互投影。"""
    run_id = payload["run_id"]
    if kind == "terminal":
        if payload["status"] == "completed":
            return payload["content"] or "处理已完成。"
        if payload["status"] == "cancelled":
            return f"任务已确认停止。\n任务：{run_id}"
        return f"任务执行失败，请查看任务状态。\n任务：{run_id}"
    if kind == "interaction":
        return format_interactions(payload["interactions"], fallback=f"任务需要回复：{run_id}")
    if payload.get("waiting_reason") == "authorization_required":
        return (
            f"任务需要重新授权才能继续，请确认后发送：\n/authorize {run_id}"
            f"\n也可取消：\n/cancel {run_id}"
        )
    return (
        f"任务暂时停顿，需要查看当前状态：\nGET /v1/runs/{run_id}\n可请求取消：\n/cancel {run_id}"
    )


def chunks(text: str, *, byte_limit: int = 3500) -> list[str]:
    """按 UTF-8 字符边界固定分片，前缀不占正文预算，SDK 不再次切分。"""
    parts, current, size = [], [], 0
    for char in text:
        width = len(char.encode("utf-8"))
        if current and size + width > byte_limit:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    parts.append("".join(current))
    if len(parts) == 1:
        return parts
    return [f"[{index + 1}/{len(parts)}]\n{part}" for index, part in enumerate(parts)]
