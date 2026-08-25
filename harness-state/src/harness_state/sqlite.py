"""SQLite JSON Snapshot StateStore。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from harness_contracts import PlanExecutionRecord

from .errors import (
    StateRecordExistsError,
    StateRecordNotFoundError,
    StateStoreError,
)
from .store import StateStore, validate_plan_id, validate_record


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_execution_records (
    plan_id TEXT PRIMARY KEY,
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class SQLiteStateStore(StateStore):
    """使用单行 JSON 快照验证原子 checkpoint 和跨进程重新加载。

    每次操作创建一个短生命周期连接，并在线程池中执行同步 ``sqlite3`` 调用，
    避免阻塞 asyncio 事件循环。阶段二定位是单进程、单 writer 参考实现。
    """

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
            f"file:financeclaw-state-{uuid4().hex}?mode=memory&cache=shared"
            if self._uri
            else database_text
        )
        # 共享内存数据库只在至少一个连接存活时存在。Keeper 不参与读写，仅保证
        # 每个短连接关闭后数据库仍可供下一次异步操作使用。
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

    async def create(self, record: PlanExecutionRecord) -> None:
        validate_record(record)
        await self._ensure_schema()
        payload = record.model_dump_json()
        now = datetime.now(UTC).isoformat()
        try:
            await asyncio.to_thread(self._create_sync, record, payload, now)
        except sqlite3.IntegrityError as exc:
            raise StateRecordExistsError(
                f"plan execution record already exists: {record.plan_id}"
            ) from exc
        except sqlite3.Error as exc:
            raise StateStoreError("failed to create SQLite state record") from exc

    async def load(self, plan_id: str) -> PlanExecutionRecord | None:
        validate_plan_id(plan_id)
        await self._ensure_schema()
        try:
            stored = await asyncio.to_thread(self._load_sync, plan_id)
        except sqlite3.Error as exc:
            raise StateStoreError("failed to load SQLite state record") from exc
        if stored is None:
            return None
        state_version, payload = stored
        try:
            record = PlanExecutionRecord.model_validate_json(payload)
        except (ValidationError, ValueError) as exc:
            raise StateStoreError(
                f"stored state payload is invalid for plan: {plan_id}"
            ) from exc
        if record.state_version != state_version:
            raise StateStoreError(
                f"stored state_version does not match payload for plan: {plan_id}"
            )
        return record

    async def save(self, record: PlanExecutionRecord) -> None:
        validate_record(record)
        await self._ensure_schema()
        payload = record.model_dump_json()
        updated_at = datetime.now(UTC).isoformat()
        try:
            changed = await asyncio.to_thread(
                self._save_sync,
                record,
                payload,
                updated_at,
            )
        except sqlite3.Error as exc:
            raise StateStoreError("failed to save SQLite state record") from exc
        if not changed:
            raise StateRecordNotFoundError(
                f"plan execution record does not exist: {record.plan_id}"
            )

    async def delete(self, plan_id: str) -> None:
        validate_plan_id(plan_id)
        await self._ensure_schema()
        try:
            await asyncio.to_thread(self._delete_sync, plan_id)
        except sqlite3.Error as exc:
            raise StateStoreError("failed to delete SQLite state record") from exc

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            try:
                await asyncio.to_thread(self._initialize_sync)
            except sqlite3.Error as exc:
                raise StateStoreError("failed to initialize SQLite StateStore") from exc
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

    def _create_sync(
        self,
        record: PlanExecutionRecord,
        payload: str,
        now: str,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO plan_execution_records (
                        plan_id, state_version, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (record.plan_id, record.state_version, payload, now, now),
                )

    def _load_sync(self, plan_id: str) -> tuple[int, str] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT state_version, payload_json
                FROM plan_execution_records WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
        return (int(row[0]), str(row[1])) if row is not None else None

    def _save_sync(
        self,
        record: PlanExecutionRecord,
        payload: str,
        updated_at: str,
    ) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE plan_execution_records
                    SET state_version = ?, payload_json = ?, updated_at = ?
                    WHERE plan_id = ?
                    """,
                    (record.state_version, payload, updated_at, record.plan_id),
                )
                return cursor.rowcount == 1

    def _delete_sync(self, plan_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM plan_execution_records WHERE plan_id = ?",
                    (plan_id,),
                )
