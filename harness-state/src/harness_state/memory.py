"""用于默认组装和单元测试的内存 StateStore。"""

from __future__ import annotations

import asyncio

from harness_contracts import PlanExecutionRecord

from .errors import StateRecordExistsError, StateRecordNotFoundError
from .store import StateStore, validate_plan_id, validate_record


class InMemoryStateStore(StateStore):
    """以 JSON 字符串保存深拷贝快照，避免调用方继续修改原 State。"""

    def __init__(self) -> None:
        self._records: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: PlanExecutionRecord) -> None:
        validate_record(record)
        payload = record.model_dump_json()
        async with self._lock:
            if record.plan_id in self._records:
                raise StateRecordExistsError(
                    f"plan execution record already exists: {record.plan_id}"
                )
            self._records[record.plan_id] = payload

    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        validate_plan_id(plan_id)
        async with self._lock:
            payload = self._records.get(plan_id)
        return PlanExecutionRecord.model_validate_json(payload) if payload is not None else None

    async def save(self, record: PlanExecutionRecord) -> None:
        validate_record(record)
        payload = record.model_dump_json()
        async with self._lock:
            if record.plan_id not in self._records:
                raise StateRecordNotFoundError(
                    f"plan execution record does not exist: {record.plan_id}"
                )
            self._records[record.plan_id] = payload

    async def delete(self, plan_id: str) -> None:
        validate_plan_id(plan_id)
        async with self._lock:
            self._records.pop(plan_id, None)
