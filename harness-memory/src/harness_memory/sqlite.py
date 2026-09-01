"""MemoryProvider 的 SQLite create-only 持久化实现。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from harness_contracts import ErrorCode, MemoryAccessError, MemoryQuery, MemoryRecord
from pydantic import ValidationError

from .canonical import matches_query, record_order
from .errors import MemoryProposalConflictError, MemoryProviderError
from .provider import (
    MemoryProvider,
    validate_memory_id,
    validate_proposal_hash,
    validate_query,
    validate_record,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    proposal_hash TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL
)
"""

_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_memory_scope_namespace_kind
    ON memory_records (tenant_id, subject_id, namespace, kind, created_at, memory_id)
    """,
)


class SQLiteMemoryProvider(MemoryProvider):
    """单进程、短连接的 SQLite MemoryProvider 参考实现。"""

    def __init__(self, database: str | Path, *, timeout_seconds: float = 5.0) -> None:
        if not isinstance(database, str | Path):
            raise TypeError("database must be a path string or Path")
        database_text = str(database)
        if not database_text.strip():
            raise ValueError("database path must not be empty")
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise TypeError("timeout_seconds must be a positive number")

        self._database = database_text
        self._uri = database_text == ":memory:"
        self._connection_target = (
            f"file:financeclaw-memory-{uuid4().hex}?mode=memory&cache=shared"
            if self._uri
            else database_text
        )
        self._keeper_connection = (
            sqlite3.connect(self._connection_target, uri=True, check_same_thread=False)
            if self._uri
            else None
        )
        self._timeout_seconds = float(timeout_seconds)
        self._initialization_lock = asyncio.Lock()
        self._initialized = False

    @property
    def database(self) -> str:
        return self._database

    async def get(self, memory_id: str) -> MemoryRecord | None:
        validate_memory_id(memory_id)
        await self._ensure_schema()
        try:
            row = await asyncio.to_thread(self._get_sync, memory_id)
        except sqlite3.Error as exc:
            raise MemoryProviderError("failed to load SQLite memory record") from exc
        if row is None:
            return None
        return _decode_record(row[0], expected_memory_id=memory_id)

    async def search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]:
        validate_query(query)
        await self._ensure_schema()
        try:
            rows = await asyncio.to_thread(self._search_sync, query)
        except sqlite3.Error as exc:
            raise MemoryProviderError("failed to search SQLite memory records") from exc

        records = tuple(
            _decode_record(payload, expected_memory_id=memory_id) for memory_id, payload in rows
        )
        return tuple(
            sorted(
                (record for record in records if matches_query(record, query)),
                key=record_order,
            )
        )

    async def put_if_absent(
        self,
        record: MemoryRecord,
        proposal_hash: str,
    ) -> MemoryRecord:
        validate_record(record)
        validate_proposal_hash(proposal_hash)
        await self._ensure_schema()
        try:
            payload, existing_hash = await asyncio.to_thread(
                self._put_if_absent_sync,
                record,
                proposal_hash,
            )
        except sqlite3.Error as exc:
            raise MemoryProviderError("failed to write SQLite memory record") from exc
        try:
            validate_proposal_hash(existing_hash)
        except (TypeError, ValueError) as exc:
            raise MemoryAccessError(
                "stored SQLite proposal hash is invalid",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
                details={"memory_id": record.memory_id},
            ) from exc
        if existing_hash != proposal_hash:
            raise MemoryProposalConflictError(
                "memory proposal identity conflicts with an existing hash",
                details={"memory_id": record.memory_id},
            )
        return _decode_record(payload, expected_memory_id=record.memory_id)

    async def delete(self, memory_id: str) -> None:
        validate_memory_id(memory_id)
        await self._ensure_schema()
        try:
            await asyncio.to_thread(self._delete_sync, memory_id)
        except sqlite3.Error as exc:
            raise MemoryProviderError("failed to delete SQLite memory record") from exc

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            try:
                await asyncio.to_thread(self._initialize_sync)
            except sqlite3.Error as exc:
                raise MemoryProviderError("failed to initialize SQLite memory store") from exc
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._connection_target,
            timeout=self._timeout_seconds,
            uri=self._uri,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout_seconds * 1000)}")
        return connection

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(_SCHEMA)
                for statement in _INDEXES:
                    connection.execute(statement)

    def _get_sync(self, memory_id: str) -> tuple[str] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM memory_records WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        return (str(row[0]),) if row is not None else None

    def _search_sync(self, query: MemoryQuery) -> tuple[tuple[str, str], ...]:
        namespaces = tuple(sorted(query.namespaces))
        kinds = tuple(sorted(kind.value for kind in query.kinds))
        namespace_slots = ",".join("?" for _ in namespaces)
        kind_slots = ",".join("?" for _ in kinds)
        statement = f"""
            SELECT memory_id, payload_json
            FROM memory_records
            WHERE tenant_id = ? AND subject_id = ?
              AND namespace IN ({namespace_slots})
              AND kind IN ({kind_slots})
            ORDER BY created_at DESC, memory_id ASC
        """
        parameters = (query.tenant_id, query.subject_id, *namespaces, *kinds)
        with closing(self._connect()) as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def _put_if_absent_sync(
        self,
        record: MemoryRecord,
        proposal_hash: str,
    ) -> tuple[str, str]:
        payload = record.model_dump_json()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT payload_json, proposal_hash
                    FROM memory_records WHERE memory_id = ?
                    """,
                    (record.memory_id,),
                ).fetchone()
                if row is not None:
                    connection.commit()
                    return str(row[0]), str(row[1])
                connection.execute(
                    """
                    INSERT INTO memory_records (
                        memory_id, proposal_hash, tenant_id, subject_id,
                        namespace, kind, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.memory_id,
                        proposal_hash,
                        record.tenant_id,
                        record.subject_id,
                        record.namespace,
                        record.kind.value,
                        payload,
                        record.created_at.isoformat(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return payload, proposal_hash

    def _delete_sync(self, memory_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM memory_records WHERE memory_id = ?",
                    (memory_id,),
                )


def _decode_record(payload: str, *, expected_memory_id: str) -> MemoryRecord:
    try:
        record = MemoryRecord.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise MemoryAccessError(
            "stored SQLite memory payload is invalid",
            code=ErrorCode.MEMORY_PROVIDER_INVALID,
            details={"memory_id": expected_memory_id},
        ) from exc
    if record.memory_id != expected_memory_id:
        raise MemoryAccessError(
            "stored SQLite memory identity does not match its row",
            code=ErrorCode.MEMORY_PROVIDER_INVALID,
            details={"memory_id": expected_memory_id},
        )
    return record
