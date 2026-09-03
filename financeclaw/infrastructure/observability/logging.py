"""结构化 JSON 日志：把日志渲染为单行 JSON 并对敏感信息做默认脱敏。

本模块属于 infrastructure 层的观测适配：全进程共用一个根 logger 的
JSON handler，保证日志可直接被采集系统解析且不泄露凭证类内容。
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

# 键名级敏感模式：字段名命中即整体替换为 [REDACTED]。
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|secret|token|api[_-]?key|credential)", re.IGNORECASE
)
# 文本级敏感模式：从自由文本中抹除 Bearer 令牌与 key=value 形式的凭证。
_SENSITIVE_TEXT = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[=:]\s*[^\s,;]+"),
)
# logging.LogRecord 的标准字段集合，用于在 format 时挑出自定义 extra 字段。
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """递归脱敏任意结构化值：键名命中即替换，字符串按模式抹除凭证。

    使用场景：``JsonLogFormatter`` 对事件文本与 extra 字段统一调用；
    也可被其他观测组件复用，作为结构化数据的最后防线。

    Args:
        value: 待脱敏的值，可为 dict、容器、字符串或标量。
        key: 当前值所属的键名，用于键名级敏感判定；递归时向下传递。

    Returns:
        脱敏后的值；容器结构保持原形，未知类型转为字符串。

    """
    # 1. 键名命中敏感模式时，无论值是什么都整体遮蔽。
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    # 2. 字典：保留键并递归脱敏每个值。
    if isinstance(value, dict):
        return {
            str(item_key): redact_sensitive(item, key=str(item_key))
            for item_key, item in value.items()
        }
    # 3. 其他容器：逐项递归脱敏（统一转为列表）。
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_sensitive(item) for item in value]
    # 4. 字符串：按 Bearer 与 key=value 两类模式抹除凭证片段。
    if isinstance(value, str):
        redacted = value
        for pattern in _SENSITIVE_TEXT:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted
    # 5. 标量原样保留，其余类型安全转为字符串。
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class JsonLogFormatter(logging.Formatter):
    """单行 JSON 日志格式化器：事件与自定义 extra 字段全部脱敏后输出。

    使用场景：由 ``configure_json_logging`` 挂载到根 logger；输出面向
    日志采集系统，保持单行以便流式解析与存储。
    """

    def format(self, record: logging.LogRecord) -> str:
        """把一条日志记录渲染为脱敏后的单行 JSON。

        Args:
            record: 待格式化的日志记录。

        Returns:
            形如 ``{"timestamp":...,"level":...}`` 的单行 JSON 字符串；
            出现异常信息时仅附带异常类型，不输出堆栈中的敏感细节。

        """
        # 1. 基础字段：时间、级别、logger 名与脱敏后的事件文本。
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_sensitive(record.getMessage()),
        }
        # 2. 附加自定义 extra 字段（排除标准字段与私有属性），逐一脱敏。
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = redact_sensitive(value, key=key)
        # 3. 异常仅记录类型名，避免堆栈文本夹带敏感内容。
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_json_logging(level: str = "INFO") -> None:
    """把根 logger 替换为单一 JSON handler 并设置日志级别。

    使用场景：bootstrap.py 组合根启动时调用，使全进程（含第三方库）
    的日志统一走 JSON 脱敏通道。

    Args:
        level: 日志级别名称，不区分大小写，默认 INFO。

    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    # 清空既有 handler，避免重复输出与非 JSON 格式混入。
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
