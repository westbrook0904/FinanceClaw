"""Audit repository port plus deterministic test/development implementation."""

from collections.abc import Iterable
from threading import Lock
from typing import Protocol

from .models import AuditRecord


class AuditRepository(Protocol):
    def append(self, record: AuditRecord) -> None: ...


class InMemoryAuditRepository:
    """Append-only repository used by tests and local development composition."""

    def __init__(self, records: Iterable[AuditRecord] = ()) -> None:
        self._records = list(records)
        self._lock = Lock()

    def append(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be AuditRecord")
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[AuditRecord, ...]:
        with self._lock:
            return tuple(self._records)
