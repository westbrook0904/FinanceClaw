"""Small immutable values used at transaction and background-task boundaries."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class CommandState(StrEnum):
    """Sending is an irreversible sending right, not an expiring work lease."""

    PREPARED = "prepared"
    SENDING = "sending"
    UNCERTAIN = "uncertain"
    SUBMITTED = "submitted"
    OBSERVED = "observed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ExecutionConflict(RuntimeError):
    """A command, grant or execution fact does not match the accepted Turn."""


class StaleTurnLease(ExecutionConflict):
    """Only the current database lease may commit background observations."""


class InteractionConflict(ExecutionConflict):
    """The answer no longer matches the active question or accepted decision."""


class TurnNotFound(LookupError):
    """A Turn is absent or belongs to another principal."""


class InteractionNotFound(LookupError):
    """An interaction is absent or belongs to another principal."""


@dataclass(frozen=True, slots=True)
class TurnLease:
    """Fencing token copied out of the short claim transaction."""

    turn_id: str
    owner: str
    epoch: int
    revision: int


def now() -> datetime:
    """Return UTC for persistence, deadlines and grant checks."""
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    """Interpret SQLite's timezone-free values using the storage UTC convention."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def digest(value: Any) -> str:
    """Hash canonical validated JSON, never an object's unstable repr."""
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def export(row: Any) -> dict[str, Any]:
    """Copy ORM columns before leaving a transaction; do not lazy-load later."""
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}
