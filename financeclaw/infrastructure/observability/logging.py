"""提供敏感字段脱敏与结构化 JSON 日志配置。"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|secret|token|api[_-]?key|credential)", re.IGNORECASE
)
_SENSITIVE_TEXT = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[=:]\s*[^\s,;]+"),
)
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """递归遍历映射和序列，按键名遮盖令牌、密钥和授权信息。"""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_sensitive(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _SENSITIVE_TEXT:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class JsonLogFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON，同时对附加字段做递归脱敏。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。
    """

    def format(self, record: logging.LogRecord) -> str:
        """把日志记录投影为单行 JSON，并递归脱敏异常和附加字段。"""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_sensitive(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = redact_sensitive(value, key=key)
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_json_logging(level: str = "INFO") -> None:
    """配置logging 模块的数据，使后续运行统一采用该设置。"""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
