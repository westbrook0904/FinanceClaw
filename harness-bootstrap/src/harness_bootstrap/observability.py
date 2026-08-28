"""Request 路由与规划阶段的安全 hash 和 best-effort Event 适配。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from harness_contracts import JsonValue
from harness_events import EventPublisher, ExecutionEvent, ExecutionEventName

_SAFE_OBSERVATION_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")


def stable_observation_hash(value: JsonValue) -> str:
    """对 JSON-safe 投影生成稳定 SHA-256，不把原值写入观察面。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_observation_code(value: object, *, fallback: str) -> str:
    """只允许短标识符进入 Event/Trace，拒绝把自由文本伪装成 reason/code。"""

    if isinstance(value, str) and _SAFE_OBSERVATION_CODE.fullmatch(value):
        return value
    return fallback


class RequestEventEmitter:
    """发布 request-level Route/Planner 事件，观察面失败不覆盖执行结果。"""

    def __init__(self, publisher: EventPublisher) -> None:
        if not isinstance(publisher, EventPublisher):
            raise TypeError("publisher must implement EventPublisher")
        self._publisher = publisher

    @property
    def publisher(self) -> EventPublisher:
        return self._publisher

    async def emit(
        self,
        name: ExecutionEventName,
        *,
        request_id: str,
        trace_id: str | None,
        plan_id: str | None = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> bool:
        try:
            await self._publisher.publish(
                ExecutionEvent(
                    name=name,
                    request_id=request_id,
                    plan_id=plan_id,
                    trace_id=trace_id,
                    attributes=attributes or {},
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True
