"""Separate current user/tool authority from finite background source derivation permits."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from financeclaw.shared.memory.models import (
    MemoryActor,
    MemoryDerivationPermit,
    MemoryPermissionError,
    utc,
)
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemorySourceRow


def require_scope(actor: MemoryActor, scope: str) -> None:
    """Scopes originate only from authenticated adapters, never a model proposal."""
    if scope not in actor.scopes and "*" not in actor.scopes:
        raise MemoryPermissionError(f"{scope} is required")


def validate_source_permit(
    session: Session,
    actor: MemoryActor,
    source: MemorySourceRow,
    *,
    owner: MemoryOwnerRow | None = None,
) -> None:
    """Recheck source permission before both model requests and SQL result commits."""
    owner = owner or session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
    if (
        owner is None
        or not owner.auto_enabled
        or source.permit_revoked
        or source.reuse_blocked
        or not source.permit
    ):
        raise MemoryPermissionError("source derivation is disabled")
    permit = MemoryDerivationPermit.model_validate(source.permit)
    if (
        permit.tenant_id,
        permit.subject_id,
        permit.source_id,
        permit.source_version,
        permit.content_hash,
    ) != (
        actor.tenant_id,
        actor.subject_id,
        source.source_id,
        source.source_version,
        source.content_hash,
    ):
        raise MemoryPermissionError("derivation permit identity mismatch")
    if (permit.data_classification.value, permit.processing_region) != (
        source.data_classification,
        source.processing_region,
    ):
        raise MemoryPermissionError("source classification or processing region changed")
    if permit.policy_revision != owner.policy_revision or utc(permit.expires_at) <= datetime.now(
        UTC
    ):
        raise MemoryPermissionError("derivation permit expired or policy changed")
    if actor.permit_source_ids and source.source_id not in actor.permit_source_ids:
        raise MemoryPermissionError("source is outside the admitted background duty")


def validate_derivation_in_session(session: Session, actor: MemoryActor, refs) -> None:
    """Use the shared evidence reader rather than privileged worker source shortcuts."""
    from financeclaw.shared.memory.evidence import EvidenceReader

    EvidenceReader().read_in_session(session, actor, tuple(refs), for_derivation=True)
