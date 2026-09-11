"""最终文本分片在首次消费时固定。"""


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
