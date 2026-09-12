"""Explicit memory queue diagnostics, controlled replay and bounded retention."""

import argparse
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import case, func, select

from financeclaw.memory_worker.bootstrap import require_schema
from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.audit.repository import SqlAlchemyAuditRepository
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.llm.memory_profiles import memory_model_profiles, memory_profile_fingerprint
from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.models import EvidenceRef, MemoryActor, MemoryConflict
from financeclaw.shared.memory.repository import canonical_hash, lock_owner, owner_filter
from financeclaw.shared.memory.tables import MemoryExtractionRow, MemoryOwnerRow, MemoryRecordRow
from financeclaw.shared.outbox.models import OutboxEvent
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow

DESTINATIONS = ("memory_extract", "memory_consolidate", "memory_index", "memory_index_delete")


class MemoryOperations:
    """Operate only explicit memory jobs; never scan and backfill all historical chats."""

    def __init__(self, sessions, *, extraction_fingerprint, consolidation_fingerprint):
        """Freeze the running deployment's supported model contracts for explicit replay."""
        self.sessions = sessions
        self.outbox = SqlAlchemyOutboxRepository(sessions)
        self.audit = SqlAlchemyAuditRepository(sessions, emit_outbox=False)
        self.fingerprints = {
            "memory_extract": extraction_fingerprint,
            "memory_consolidate": consolidation_fingerprint,
        }

    def status(self):
        """Aggregate queue and budget metadata without disclosing source or memory bodies."""
        with self.sessions() as session:
            rows = session.execute(
                select(
                    OutboxEventRow.destination,
                    OutboxEventRow.status,
                    func.count(),
                    func.min(OutboxEventRow.available_at),
                    func.max(OutboxEventRow.published_at),
                    func.sum(
                        OutboxEventRow.processing_metadata["model_budget"]["attempts"].as_integer()
                    ),
                    func.sum(
                        OutboxEventRow.processing_metadata["model_budget"][
                            "reserved_input_tokens"
                        ].as_integer()
                    ),
                    func.sum(
                        OutboxEventRow.processing_metadata["model_budget"][
                            "reserved_output_tokens"
                        ].as_integer()
                    ),
                    func.sum(OutboxEventRow.processing_metadata["version_conflicts"].as_integer()),
                    func.sum(OutboxEventRow.processing_metadata["lease_takeovers"].as_integer()),
                    func.sum(
                        case(
                            (
                                OutboxEventRow.processing_metadata["purge_status"].as_string()
                                == "pending",
                                1,
                            ),
                            else_=0,
                        )
                    ),
                )
                .where(OutboxEventRow.destination.in_(DESTINATIONS))
                .group_by(OutboxEventRow.destination, OutboxEventRow.status)
            )
            return [
                {
                    "destination": row[0],
                    "status": row[1],
                    "count": row[2],
                    "oldest_available_at": row[3],
                    "last_completed_at": row[4],
                    "model_attempts": row[5] or 0,
                    "reserved_input_tokens": row[6] or 0,
                    "reserved_output_tokens": row[7] or 0,
                    "version_conflicts": row[8] or 0,
                    "lease_takeovers": row[9] or 0,
                    "purge_pending": row[10] or 0,
                }
                for row in rows
            ]

    def replay(self, event_id: str, *, operator: str, reason: str, new_model_budget=False):
        """Create an audited fresh event; new budget requires an explicit operator flag."""
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required for controlled replay")
        original = self.outbox.get(event_id)
        if original.destination not in self.fingerprints:
            raise ValueError("replay supports extraction and consolidation jobs only")
        if (
            original.payload.get("pipeline_version") != "memory/1"
            or original.payload.get("model_profile_version")
            != self.fingerprints[original.destination]
        ):
            raise MemoryConflict("old model or pipeline needs explicit authorized reconstruction")
        actor = MemoryActor(
            tenant_id=original.tenant_id, subject_id=original.subject_id, kind="worker"
        )
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            row = session.get(OutboxEventRow, event_id)
            if row.status != "dead_letter":
                raise MemoryConflict("only a dead-letter event can be replayed")
            payload = deepcopy(row.payload)
            metadata = deepcopy(row.processing_metadata or {})
            if original.destination == "memory_extract":
                refs = tuple(EvidenceRef.model_validate(ref) for ref in payload["sources"])
                if payload.get("part_index") is not None:
                    # A partial cohort cannot be made complete by replaying one lost
                    # part under a new identity. Reconstruct the exact original closure.
                    parent = session.get(OutboxEventRow, payload["prepare_event_id"])
                    if parent is None:
                        raise MemoryConflict("original preparation event is unavailable")
                    payload = deepcopy(parent.payload)
                    refs = tuple(EvidenceRef.model_validate(ref) for ref in payload["sources"])
                    if not new_model_budget:
                        raise MemoryConflict(
                            "reconstructing a failed closure requires explicit new model budget"
                        )
                evidence_actor = actor.model_copy(
                    update={"permit_source_ids": tuple(ref.source_id for ref in refs)}
                )
                EvidenceReader().read_in_session(session, evidence_actor, refs, for_derivation=True)
                event_type = "memory.extract.prepare"
            else:
                if owner.active_consolidation_event_id:
                    raise MemoryConflict("owner already has an active consolidation job")
                ids = metadata.get("snapshot_ids", [])
                parts = list(
                    session.scalars(
                        select(MemoryExtractionRow).where(
                            *owner_filter(MemoryExtractionRow, actor),
                            MemoryExtractionRow.extraction_id.in_(ids),
                            MemoryExtractionRow.disposition == "quarantined",
                        )
                    )
                )
                if not parts or len(parts) != len(ids) or any(not part.output for part in parts):
                    raise MemoryConflict("original inputs have been invalidated or are unavailable")
                groups = {}
                for part in parts:
                    groups.setdefault(part.closure_hash, []).append(part)
                for group in groups.values():
                    refs = tuple(
                        EvidenceRef.model_validate(ref) for part in group for ref in part.evidence
                    )
                    evidence_actor = actor.model_copy(
                        update={"permit_source_ids": tuple(ref.source_id for ref in refs)}
                    )
                    EvidenceReader().read_in_session(
                        session, evidence_actor, refs, for_derivation=True
                    )
                    owner.extraction_revision += 1
                    for part in group:
                        part.extraction_revision = owner.extraction_revision
                        part.disposition = "pending"
                        part.disposition_reason = None
                        part.consumed_at = None
                payload["requested_revision"] = owner.extraction_revision
                event_type = "memory.consolidate.requested"
            new_id = "memory-replay-" + uuid4().hex
            control = {"replayed_from": event_id, "new_model_budget": new_model_budget}
            if not new_model_budget:
                for key in ("model_budget", "model_usage"):
                    if key in metadata:
                        control[key] = metadata[key]
            if original.destination == "memory_consolidate":
                owner.active_consolidation_event_id = new_id
            self.outbox.enqueue_in_session(
                session,
                OutboxEvent(
                    event_id=new_id,
                    event_type=event_type,
                    destination=original.destination,
                    aggregate_type=original.aggregate_type,
                    aggregate_id=original.aggregate_id,
                    tenant_id=actor.tenant_id,
                    subject_id=actor.subject_id,
                    payload=payload,
                    processing_metadata=control,
                ),
            )
            self.audit.append_in_session(
                session,
                AuditRecord(
                    audit_id="memory-replay-audit-" + new_id,
                    event_type=AuditEventType.MEMORY_JOB_REPLAYED,
                    tenant_id=actor.tenant_id,
                    subject_id=actor.subject_id,
                    resource_type="memory_job",
                    resource_id=event_id,
                    resource_version=str(row.claim_epoch),
                    action="replay",
                    decision="explicit_operator",
                    policy_version="memory/1",
                    payload_hash=canonical_hash(
                        [event_id, new_id, operator, reason, new_model_budget]
                    ),
                    metadata={
                        "operator": operator,
                        "reason": reason[:1000],
                        "replacement_event_id": new_id,
                        "new_model_budget": new_model_budget,
                    },
                ),
            )
            return new_id

    def reindex(
        self, *, tenant_id, subject_id, operator, reason, limit=100, after=None, request_id=None
    ):
        """Queue a bounded SQL-authoritative page under a repeatable rebuild request identity."""
        if not 1 <= limit <= 1000 or not operator.strip() or not reason.strip():
            raise ValueError("bounded limit, operator and reason are required")
        actor = MemoryActor(tenant_id=tenant_id, subject_id=subject_id, kind="worker")
        request_id = request_id or uuid4().hex
        with self.sessions.begin() as session:
            owner = lock_owner(session, actor)
            statement = select(MemoryRecordRow).where(
                *owner_filter(MemoryRecordRow, actor),
                MemoryRecordRow.is_current.is_(True),
                MemoryRecordRow.status == "active",
                MemoryRecordRow.kind == "task",
            )
            if after:
                statement = statement.where(MemoryRecordRow.memory_id > after)
            records = list(
                session.scalars(statement.order_by(MemoryRecordRow.memory_id).limit(limit))
            )
            scheduled = 0
            for record in records:
                if record.expires_at and record.expires_at.replace(
                    tzinfo=record.expires_at.tzinfo or UTC
                ) <= datetime.now(UTC):
                    continue
                try:
                    EvidenceReader().read_in_session(
                        session,
                        actor,
                        tuple(EvidenceRef.model_validate(ref) for ref in record.evidence),
                    )
                except (LookupError, PermissionError, ValueError):
                    continue
                event_id = "memory-reindex-" + canonical_hash(
                    [tenant_id, subject_id, request_id, record.memory_id, record.revision]
                )
                if session.get(OutboxEventRow, event_id) is not None:
                    scheduled += 1
                    continue
                self.outbox.enqueue_in_session(
                    session,
                    OutboxEvent(
                        event_id=event_id,
                        event_type="memory.index.requested",
                        destination="memory_index",
                        aggregate_type="memory",
                        aggregate_id=record.memory_id,
                        tenant_id=tenant_id,
                        subject_id=subject_id,
                        payload={
                            "memory_id": record.memory_id,
                            "revision": record.revision,
                            "privacy_epoch": owner.privacy_epoch,
                            "index_version": "memory-v1",
                        },
                    ),
                )
                scheduled += 1
            audit_key = canonical_hash([tenant_id, subject_id, request_id, after, limit])
            self.audit.append_in_session(
                session,
                AuditRecord(
                    audit_id="memory-reindex-audit-" + audit_key,
                    event_type=AuditEventType.MEMORY_REINDEX_REQUESTED,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    resource_type="memory_index",
                    resource_id=request_id,
                    resource_version="memory-v1",
                    action="reindex",
                    decision="explicit_operator",
                    policy_version="memory/1",
                    payload_hash=canonical_hash([audit_key, operator, reason]),
                    metadata={
                        "operator": operator,
                        "reason": reason[:1000],
                        "scheduled": scheduled,
                    },
                ),
            )
            return {
                "request_id": request_id,
                "scheduled": scheduled,
                "next_after": records[-1].memory_id if len(records) == limit else None,
            }

    def retain(self, *, limit=100, apply=False):
        """Preview or remove old unreferenced processed outputs and published events only."""
        if not 1 <= limit <= 1000:
            raise ValueError("retention limit must be between 1 and 1000")
        now = datetime.now(UTC)
        deleted = {"extractions": 0, "outbox": 0, "apply": apply}
        with self.sessions.begin() as session:
            parts = list(
                session.scalars(
                    select(MemoryExtractionRow)
                    .where(
                        MemoryExtractionRow.disposition == "consumed",
                        MemoryExtractionRow.consumed_at < now - timedelta(days=30),
                    )
                    .order_by(MemoryExtractionRow.consumed_at)
                    .limit(limit)
                )
            )
            parts.sort(key=lambda part: (part.tenant_id, part.subject_id, part.extraction_id))
            for part in parts:
                actor = MemoryActor(tenant_id=part.tenant_id, subject_id=part.subject_id)
                lock_owner(session, actor)
                records = session.scalars(
                    select(MemoryRecordRow.evidence_source_ids)
                    .where(
                        *owner_filter(MemoryRecordRow, actor),
                        MemoryRecordRow.is_current.is_(True),
                        MemoryRecordRow.status.in_(["active", "proposed"]),
                    )
                    .limit(1001)
                )
                references = list(records)
                if len(references) > 1000 or any(
                    set(part.evidence_source_ids).intersection(refs) for refs in references
                ):
                    continue
                deleted["extractions"] += 1
                if apply:
                    session.delete(part)
            session.flush()
            rows = list(
                session.scalars(
                    select(OutboxEventRow)
                    .where(
                        OutboxEventRow.destination.in_(DESTINATIONS),
                        OutboxEventRow.status == "published",
                        OutboxEventRow.published_at < now - timedelta(days=7),
                    )
                    .order_by(OutboxEventRow.published_at)
                    .limit(limit)
                )
            )
            for row in rows:
                metadata = row.processing_metadata or {}
                if any(
                    value in {"unknown", "in_flight"}
                    for value in metadata.get("index_writes", {}).values()
                ):
                    continue
                if session.scalar(
                    select(MemoryOwnerRow.subject_id)
                    .where(MemoryOwnerRow.active_consolidation_event_id == row.event_id)
                    .limit(1)
                ):
                    continue
                if session.scalar(
                    select(MemoryExtractionRow.extraction_id)
                    .where(MemoryExtractionRow.prepare_event_id == row.event_id)
                    .limit(1)
                ):
                    continue
                if session.scalar(
                    select(OutboxEventRow.event_id)
                    .where(
                        OutboxEventRow.payload["prepare_event_id"].as_string() == row.event_id,
                        OutboxEventRow.status.in_(["pending", "publishing", "dead_letter"]),
                    )
                    .limit(1)
                ):
                    continue
                deleted["outbox"] += 1
                if apply:
                    session.delete(row)
            return deleted


def main():
    """Expose explicit operator commands with dry-run retention and no implicit budget reset."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    replay = commands.add_parser("replay")
    replay.add_argument("event_id")
    replay.add_argument("--operator", required=True)
    replay.add_argument("--reason", required=True)
    replay.add_argument("--new-model-budget", action="store_true")
    retain = commands.add_parser("retain")
    retain.add_argument("--limit", type=int, default=100)
    retain.add_argument("--apply", action="store_true")
    reindex = commands.add_parser("reindex")
    reindex.add_argument("--tenant-id", required=True)
    reindex.add_argument("--subject-id", required=True)
    reindex.add_argument("--operator", required=True)
    reindex.add_argument("--reason", required=True)
    reindex.add_argument("--limit", type=int, default=100)
    reindex.add_argument("--after")
    reindex.add_argument("--request-id")
    args = parser.parse_args()
    settings = FinanceClawSettings(_env_file=None)
    profiles = memory_model_profiles(settings)
    database = ApplicationDatabase(settings.database_url.get_secret_value())
    try:
        require_schema(database)
        operations = MemoryOperations(
            database.session_factory,
            extraction_fingerprint=memory_profile_fingerprint(profiles[0]),
            consolidation_fingerprint=memory_profile_fingerprint(profiles[1]),
        )
        if args.command == "status":
            result = operations.status()
        elif args.command == "replay":
            result = operations.replay(
                args.event_id,
                operator=args.operator,
                reason=args.reason,
                new_model_budget=args.new_model_budget,
            )
        elif args.command == "reindex":
            result = operations.reindex(
                tenant_id=args.tenant_id,
                subject_id=args.subject_id,
                operator=args.operator,
                reason=args.reason,
                limit=args.limit,
                after=args.after,
                request_id=args.request_id,
            )
        else:
            result = operations.retain(limit=args.limit, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        database.close()


if __name__ == "__main__":
    main()
