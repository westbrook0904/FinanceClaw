"""在日志和公开交互中使用的递归凭据脱敏规则。"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|dsn|credential|reasoning|thinking|hidden)",
    re.IGNORECASE,
)


_SECRET_VALUES = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk|lsv2_pt)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?:postgres(?:ql)?|redis)://[^\s]+", re.IGNORECASE),
)


def redact_sensitive(value: Any) -> Any:
    """递归脱敏任意结构：命中敏感键的映射值与命中凭证形态的字符串被替换。

    Args:
        value: 任意可递归遍历的结构（映射、序列、字符串或标量）。

    Returns:
        Any: 与输入同构的脱敏副本；敏感键值与凭证子串替换为 ``<redacted>``。

    """
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEYS.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        for pattern in _SECRET_VALUES:
            value = pattern.sub("<redacted>", value)
    return value
