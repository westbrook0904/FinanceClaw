"""最终 Markdown 卡片在首次消费时固定，长代码块跨卡片保持闭合。"""

import re


def chunks(text: str, *, byte_limit: int = 3500, numbered: bool = True) -> list[str]:
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
    if len(parts) == 1 or not numbered:
        return parts
    return [f"[{index + 1}/{len(parts)}]\n{part}" for index, part in enumerate(parts)]


def answer_cards(text: str, *, byte_limit: int = 12000) -> list[dict]:
    """按行保留 Markdown；超长行按字符切分，围栏代码在每片关闭并于下片重开。"""
    parts, current = [], ""
    fence, opening = "", ""
    for line in text.splitlines(keepends=True):
        for piece in chunks(line, byte_limit=byte_limit, numbered=False):
            if current and len((current + piece).encode()) > byte_limit:
                parts.append(current + ("\n" + fence if fence else ""))
                current = opening + "\n" if fence else ""
            current += piece
        match = re.match(r"^ {0,3}(`{3,}|~{3,})([^\n]*)", line)
        if match:
            marker, suffix = match.groups()
            if not fence:
                fence, opening = marker, line.rstrip("\r\n")
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence, opening = "", ""
    if current:
        parts.append(current + ("\n" + fence if fence else ""))
    parts = parts or ["处理已完成。"]
    cards = []
    for index, content in enumerate(parts):
        title = "回复" if len(parts) == 1 else f"回复（{index + 1}/{len(parts)}）"
        cards.append(
            {
                "schema": "2.0",
                "config": {"update_multi": True, "summary": {"content": title}},
                "body": {"elements": [{"tag": "markdown", "content": content}]},
                **(
                    {"header": {"title": {"tag": "plain_text", "content": title}}}
                    if len(parts) > 1
                    else {}
                ),
            }
        )
    return cards
