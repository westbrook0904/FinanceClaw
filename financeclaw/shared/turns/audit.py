"""Immutable grant and observation evidence, stored in the existing audit journal."""

from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.turns.types import aware, digest


def record_fact(session, turn, kind, revision, payload):
    """Append one idempotent audit fact inside the product transaction."""
    identifier = digest([turn.turn_id, kind, revision])
    if session.get(AuditRecordRow, identifier) is None:
        session.add(
            AuditRecordRow(
                audit_id=identifier,
                event_type="turn." + kind,
                tenant_id=turn.tenant_id,
                subject_id=turn.subject_id,
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                resource_type="turn",
                resource_id=turn.turn_id,
                resource_version=str(revision),
                action=kind,
                decision="recorded",
                policy_version="stage10",
                payload_hash=digest(payload),
                metadata_json=payload,
            )
        )


def record_grant(session, turn):
    """Retain each finite grant version without introducing a separate authorization table."""
    record_fact(
        session,
        turn,
        "authorization",
        turn.grant_revision,
        {
            "revision": turn.grant_revision,
            "scopes": turn.grant_scopes,
            "source": turn.grant_source,
            "source_hash": turn.grant_source_hash,
            "issued_at": aware(turn.grant_issued_at).isoformat(),
            "expires_at": aware(turn.grant_expires_at).isoformat(),
            "revoked": turn.grant_revoked,
        },
    )
