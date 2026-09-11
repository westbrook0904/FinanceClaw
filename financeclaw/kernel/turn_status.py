"""Product Turn lifecycle values shared across package boundaries."""

from enum import StrEnum


class TurnStatus(StrEnum):
    """Product lifecycle; native attempt status is deliberately a separate value."""

    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    RESUMING = "resuming"
    CANCELLING = "cancelling"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
