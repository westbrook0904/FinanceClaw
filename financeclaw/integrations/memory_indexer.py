"""Version-specific Store projections whose authority always remains in SQL."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.models import EvidenceRef, MemoryActor, MemoryPermissionError, utc
from financeclaw.shared.memory.namespace import memory_index_namespace
from financeclaw.shared.memory.repository import owner_filter
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemoryRecordRow
from financeclaw.shared.outbox.tables import OutboxEventRow

MEMORY_INDEX_DESTINATION = "memory_index"
MEMORY_INDEX_DELETE_DESTINATION = "memory_index_delete"


class PendingIndexWrites(RuntimeError):
    """An uncertain remote write prevents claiming that physical copies are verified gone."""


class MemoryIndexer:
    """Build server-owned projections and fence stale revisions before and after I/O."""

    def __init__(self, sessions, store, outbox, *, index_version="memory-v1"):
        """Inject restricted Store credentials; models never provide namespace or content."""
        self.sessions = sessions
        self.store = store
        self.outbox = outbox
        self.index_version = index_version

    async def publish(self, event):
        """Project an active exact revision or delete its exact obsolete key."""
        expected = {
            "memory.index.requested": MEMORY_INDEX_DESTINATION,
            "memory.index.delete": MEMORY_INDEX_DELETE_DESTINATION,
        }
        if expected.get(event.event_type) != event.destination:
            raise ValueError("wrong memory index event")
        version = event.payload["index_version"]
        if version != self.index_version:
            raise ValueError("memory index version requires explicit rebuild")
        actor = MemoryActor(tenant_id=event.tenant_id, subject_id=event.subject_id, kind="worker")
        namespace = memory_index_namespace(actor, version)
        key = f"{event.payload['memory_id']}:{int(event.payload['revision'])}"
        if event.event_type == "memory.index.delete":
            await self.store.delete_item(namespace, key)
            pending = await asyncio.to_thread(self._unknown_writes, event)
            await asyncio.to_thread(
                self._set_metadata, event, {"purge_status": "pending" if pending else "verified"}
            )
            if pending:
                raise PendingIndexWrites("remote write outcome remains unknown")
            return
        projection = await asyncio.to_thread(self._start_write, event, actor)
        if projection is None:
            await self.store.delete_item(namespace, key)
            return
        try:
            await self.store.put_item(namespace, key, projection, index=["content"])
        except BaseException:
            # A timeout or cancellation does not prove the remote Store rejected the
            # request. Preserve uncertainty even after this publisher loses its lease.
            await asyncio.shield(asyncio.to_thread(self._observe_write, event, "unknown"))
            raise
        else:
            await asyncio.to_thread(self._observe_write, event, "acknowledged")
        current = await asyncio.to_thread(self._projection, event, actor)
        if current is None:
            await self.store.delete_item(namespace, key)
            await asyncio.to_thread(self._set_metadata, event, {"obsolete_key_removed": True})

    def _projection(self, event, actor):
        """Owner and exact SQL revision are checked before any body leaves the database."""
        with self.sessions() as session:
            return self._projection_in_session(session, event, actor)

    def _projection_in_session(self, session, event, actor):
        """Rebuild from current SQL eligibility even if an unrelated forget changed owner epoch."""
        row = session.scalar(
            select(MemoryRecordRow).where(
                *owner_filter(MemoryRecordRow, actor),
                MemoryRecordRow.memory_id == event.payload["memory_id"],
                MemoryRecordRow.revision == event.payload["revision"],
            )
        )
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        if (
            row is None
            or owner is None
            or not row.is_current
            or row.status != "active"
            or row.kind != "task"
        ):
            return None
        if row.expires_at and utc(row.expires_at) <= datetime.now(UTC):
            return None
        try:
            EvidenceReader().read_in_session(
                session, actor, tuple(EvidenceRef.model_validate(value) for value in row.evidence)
            )
        except (MemoryPermissionError, LookupError, ValueError):
            return None
        return {
            "memory_id": row.memory_id,
            "revision": row.revision,
            "content": row.content,
            "content_hash": row.content_hash,
            "scope_type": row.scope_type,
            "scope_id": row.scope_id,
            "created_at": utc(row.created_at).isoformat(),
            "privacy_epoch": owner.privacy_epoch,
            "index_version": self.index_version,
        }

    def _start_write(self, event, actor):
        """Persist remote I/O intent before submitting bytes to the Store."""
        with self.sessions.begin() as session:
            owner = session.scalar(
                select(MemoryOwnerRow).where(*owner_filter(MemoryOwnerRow, actor)).with_for_update()
            )
            if owner is None:
                return None
            projection = self._projection_in_session(session, event, actor)
            if projection is None:
                return None
            row = self.outbox.require_claim_in_session(session, event.event_id, event.claim_epoch)
            metadata = dict(row.processing_metadata or {})
            attempts = dict(metadata.get("index_writes", {}))
            attempts[str(event.claim_epoch)] = "in_flight"
            metadata["index_writes"] = attempts
            row.processing_metadata = metadata
            return projection

    def _observe_write(self, event, outcome):
        """Update only the acknowledged attempt observation, never business facts."""
        with self.sessions.begin() as session:
            row = session.scalar(
                select(OutboxEventRow)
                .where(
                    OutboxEventRow.event_id == event.event_id,
                    OutboxEventRow.tenant_id == event.tenant_id,
                    OutboxEventRow.subject_id == event.subject_id,
                )
                .with_for_update()
            )
            if row is None:
                return
            metadata = dict(row.processing_metadata or {})
            attempts = dict(metadata.get("index_writes", {}))
            if str(event.claim_epoch) in attempts:
                attempts[str(event.claim_epoch)] = outcome
            metadata["index_writes"] = attempts
            row.processing_metadata = metadata

    def _set_metadata(self, event, values):
        """Only the live publisher may describe its own cleanup completion."""
        with self.sessions.begin() as session:
            self.outbox.update_metadata_in_session(
                session, event.event_id, event.claim_epoch, values
            )

    def _unknown_writes(self, event):
        """Keep uncertain historical writes visible rather than equating delete with purge."""
        with self.sessions.begin() as session:
            actor = MemoryActor(
                tenant_id=event.tenant_id, subject_id=event.subject_id, kind="worker"
            )
            session.scalar(
                select(MemoryOwnerRow).where(*owner_filter(MemoryOwnerRow, actor)).with_for_update()
            )
            rows = session.scalars(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == event.tenant_id,
                    OutboxEventRow.subject_id == event.subject_id,
                    OutboxEventRow.destination == MEMORY_INDEX_DESTINATION,
                    OutboxEventRow.event_type == "memory.index.requested",
                    OutboxEventRow.aggregate_id == event.aggregate_id,
                )
            )
            for row in rows:
                if (
                    row.payload.get("memory_id") != event.payload["memory_id"]
                    or row.payload.get("revision") != event.payload["revision"]
                ):
                    continue
                if any(
                    status in {"in_flight", "unknown"}
                    for status in (row.processing_metadata or {}).get("index_writes", {}).values()
                ):
                    return True
            return False
