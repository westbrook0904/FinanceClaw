"""Runtime context used by the isolated Stage-0 graph."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpikeContext:
    """Trusted runtime values that are never inferred from the prompt."""

    request_id: str = "stage0-local"
    allow_write: bool = True
    environment: str = "development"
