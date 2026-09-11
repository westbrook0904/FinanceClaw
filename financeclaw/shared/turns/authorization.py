"""Finite authorization checks shared by admission, command dispatch and graph execution."""

from datetime import datetime, timedelta

from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.shared.turns.tables import ConversationTurnRow
from financeclaw.shared.turns.types import ExecutionConflict, aware, digest, now


def intersect_scopes(original, current) -> frozenset[str]:
    """Intersect grants without letting a wildcard expand the other grant."""
    original, current = frozenset(original), frozenset(current)
    return current if "*" in original else original if "*" in current else original & current


def require_scopes(granted, required) -> None:
    """Reject a missing capability before any protected operation."""
    if "*" not in granted and not frozenset(required).issubset(granted):
        raise PermissionError("required execution scope is missing")


def check_authorization(
    turn: ConversationTurnRow, *, scopes=None, at: datetime | None = None
) -> None:
    """Validate the live grant separately from the immutable release upper bound."""
    if turn.grant_revoked or aware(turn.grant_expires_at) <= (at or now()):
        raise ExecutionConflict("task authorization expired or revoked")
    if (
        scopes is not None
        and "*" not in turn.grant_scopes
        and not set(scopes).issubset(turn.grant_scopes)
    ):
        raise ExecutionConflict("execution scopes exceed current task authorization")


def bounded_authorization(
    settings,
    *,
    tenant_id: str,
    subject_id: str,
    scopes: frozenset[str],
    evidence: AuthorizationEvidence | None,
):
    """Bound a trusted principal's evidence by the product grant duration."""
    at = now()
    if evidence is None:
        if settings.environment.value in {"production", "staging"}:
            raise PermissionError("trusted authorization evidence is required")
        evidence = AuthorizationEvidence(
            source="development",
            source_hash=digest([tenant_id, subject_id, sorted(scopes)]),
            issued_at=at,
            expires_at=at + timedelta(seconds=settings.turn_grant_seconds),
        )
    expires = min(aware(evidence.expires_at), at + timedelta(seconds=settings.turn_grant_seconds))
    if (
        expires <= at
        or aware(evidence.issued_at) > at + timedelta(seconds=5)
        or evidence.source == "development"
        and settings.environment.value not in {"development", "test"}
    ):
        raise PermissionError("authorization evidence is expired or not yet valid")
    return evidence, expires
