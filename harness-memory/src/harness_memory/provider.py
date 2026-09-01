"""MemoryProvider 的 ID-only 存储 SPI。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import MemoryQuery, MemoryRecord


class MemoryProvider(ABC):
    """只负责持久化；scope authorization 必须由 MemoryGateway 完成。"""

    @abstractmethod
    async def get(self, memory_id: str) -> MemoryRecord | None:
        """按内部 ID 获取记录；不执行授权。"""

    @abstractmethod
    async def search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]:
        """返回确定性过滤结果；Gateway 负责最终边界和裁剪。"""

    @abstractmethod
    async def put_if_absent(
        self,
        record: MemoryRecord,
        proposal_hash: str,
    ) -> MemoryRecord:
        """create-only 写入；相同 identity/hash 幂等，不同 hash 冲突。"""

    @abstractmethod
    async def delete(self, memory_id: str) -> None:
        """按内部 ID 幂等删除；不执行授权。"""


def validate_memory_id(memory_id: str) -> str:
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise TypeError("memory_id must be a non-empty string")
    if memory_id != memory_id.strip():
        raise ValueError("memory_id must not contain surrounding whitespace")
    return memory_id


def validate_record(record: MemoryRecord) -> MemoryRecord:
    if not isinstance(record, MemoryRecord):
        raise TypeError("record must be MemoryRecord")
    return record


def validate_query(query: MemoryQuery) -> MemoryQuery:
    if not isinstance(query, MemoryQuery):
        raise TypeError("query must be MemoryQuery")
    return query


def validate_proposal_hash(proposal_hash: str) -> str:
    if (
        not isinstance(proposal_hash, str)
        or len(proposal_hash) != 64
        or any(character not in "0123456789abcdef" for character in proposal_hash)
    ):
        raise TypeError("proposal_hash must be a lowercase SHA-256 hash")
    return proposal_hash
