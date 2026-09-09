"""Public run errors shared by application owners and HTTP adapters."""


class IdempotencyConflict(RuntimeError):
    """An idempotency key was reused for different input."""


class RunNotFound(LookupError):
    """The run is absent or does not belong to the requesting identity."""
