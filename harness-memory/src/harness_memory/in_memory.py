"""MemoryProvider 的进程内 create-only 实现。"""

from __future__ import annotations

import asyncio

from harness_contracts import MemoryQuery, MemoryRecord

from .canonical import matches_query, record_order
from .errors import MemoryProposalConflictError
from .provider import (
    MemoryProvider,
    validate_memory_id,
    validate_proposal_hash,
    validate_query,
    validate_record,
)


class InMemoryMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._records: dict[str, tuple[MemoryRecord, str]] = {}
        self._lock = asyncio.Lock()

    async def get(self, memory_id: str) -> MemoryRecord | None:
        validate_memory_id(memory_id)
        async with self._lock:
            stored = self._records.get(memory_id)
            return stored[0] if stored is not None else None

    async def search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]:
        validate_query(query)
        async with self._lock:
            records = tuple(record for record, _ in self._records.values())
        return tuple(
            sorted((record for record in records if matches_query(record, query)), key=record_order)
        )

    async def put_if_absent(
        self,
        record: MemoryRecord,
        proposal_hash: str,
    ) -> MemoryRecord:
        validate_record(record)
        validate_proposal_hash(proposal_hash)
        async with self._lock:
            existing = self._records.get(record.memory_id)
            if existing is not None:
                existing_record, existing_hash = existing
                if existing_hash != proposal_hash:
                    raise MemoryProposalConflictError(
                        "memory proposal identity conflicts with an existing hash",
                        details={"memory_id": record.memory_id},
                    )
                return existing_record
            self._records[record.memory_id] = (record, proposal_hash)
            return record

    async def delete(self, memory_id: str) -> None:
        validate_memory_id(memory_id)
        async with self._lock:
            self._records.pop(memory_id, None)

    async def count(self) -> int:
        async with self._lock:
            return len(self._records)
