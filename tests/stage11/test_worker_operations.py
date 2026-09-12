"""Explicit replay preserves authorization and retention protects durable references."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from financeclaw.memory_worker.operations import MemoryOperations
from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.memory.models import MemoryConflict
from financeclaw.shared.outbox.tables import OutboxEventRow
from tests.stage11.test_worker_outbox import worker_outbox as outbox_fixture
from tests.stage11.test_worker_pipeline import extract, ready_consolidation
from tests.stage11.test_worker_pipeline import pipeline as pipeline_fixture

worker_outbox = outbox_fixture
pipeline = pipeline_fixture


def test_retention_dry_run_and_uncertain_remote_write_protection(worker_outbox):
    """Published unknown Store attempts remain available for later physical purge reconciliation."""
    database, outbox = worker_outbox
    event = outbox.claim_pending(destination="memory_extract", limit=1)[0]
    outbox.mark_published(event.event_id, claim_epoch=event.claim_epoch)
    with database.session_factory.begin() as session:
        row = session.get(OutboxEventRow, event.event_id)
        row.published_at = datetime.now(UTC) - timedelta(days=8)
    operations = MemoryOperations(
        database.session_factory, extraction_fingerprint="f", consolidation_fingerprint="f"
    )
    assert operations.retain()["outbox"] == 1
    assert outbox.get(event.event_id).status == "published"
    with database.session_factory.begin() as session:
        session.get(OutboxEventRow, event.event_id).processing_metadata = {
            "index_writes": {"1": "unknown"}
        }
    assert operations.retain(apply=True)["outbox"] == 0
    with database.session_factory.begin() as session:
        session.get(OutboxEventRow, event.event_id).processing_metadata = {}
    assert operations.retain(apply=True)["outbox"] == 1
    with pytest.raises(LookupError):
        outbox.get(event.event_id)


@pytest.mark.asyncio
async def test_explicit_consolidation_replay_has_new_ready_revision_and_audit(pipeline):
    """Replaying exact quarantined inputs is explicit and never extends source permission."""
    database, outbox, model, _, _, _, _ = pipeline
    await extract(pipeline, {"suggestions": []})
    event = ready_consolidation(pipeline)
    from financeclaw.memory_worker.consolidation import ConsolidationHandler

    handler = ConsolidationHandler(database.session_factory, outbox, model)
    handler._snapshot(event)
    outbox.reserve_model_attempt(
        event.event_id,
        claim_epoch=event.claim_epoch,
        max_attempts=4,
        input_tokens=20,
        output_tokens=30,
    )
    handler.fail(event, "fixture_failure", terminal=True)
    operations = MemoryOperations(
        database.session_factory,
        extraction_fingerprint=model.fingerprint,
        consolidation_fingerprint=model.fingerprint,
    )
    replacement_id = operations.replay(
        event.event_id, operator="local-test", reason="controlled retry"
    )
    replacement = outbox.get(replacement_id)
    assert replacement.processing_metadata["model_budget"]["attempts"] == 1
    assert replacement.payload["requested_revision"] == 2
    with database.session_factory() as session:
        assert (
            session.scalar(
                select(AuditRecordRow).where(AuditRecordRow.event_type == "memory.job_replayed")
            )
            is not None
        )
    with pytest.raises(MemoryConflict, match="active"):
        operations.replay(event.event_id, operator="local-test", reason="repeat")


def test_status_reports_queue_counts_without_payload(worker_outbox):
    """Diagnostics disclose statuses and token reservations, never source text or credentials."""
    database, _ = worker_outbox
    operations = MemoryOperations(
        database.session_factory, extraction_fingerprint="f", consolidation_fingerprint="f"
    )
    rows = operations.status()
    assert rows[0]["destination"] == "memory_extract" and rows[0]["count"] == 1
    assert "payload" not in rows[0]
