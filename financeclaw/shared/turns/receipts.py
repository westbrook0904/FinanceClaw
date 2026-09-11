"""Low-volume human control receipts are immutable audit facts, not another queue."""

from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.turns.types import ExecutionConflict, digest


def read_receipt(session, turn, key, fingerprint):
    """Read an immutable control receipt and reject key reuse with different content."""
    row = session.get(AuditRecordRow, digest([turn.turn_id, "control", key]))
    if row is None:
        return None
    if row.payload_hash != fingerprint:
        raise ExecutionConflict("control key was used with different content")
    return row.metadata_json["result"]


def save_receipt(session, turn, key, fingerprint, result):
    """Append the accepted control result to the existing audit journal."""
    session.add(
        AuditRecordRow(
            audit_id=digest([turn.turn_id, "control", key]),
            event_type="turn.control_accepted",
            tenant_id=turn.tenant_id,
            subject_id=turn.subject_id,
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            resource_type="turn",
            resource_id=turn.turn_id,
            resource_version="1",
            action="control",
            decision="accepted",
            policy_version="stage10",
            payload_hash=fingerprint,
            metadata_json={"result": result},
        )
    )
