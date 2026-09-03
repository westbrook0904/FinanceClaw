import io
import json

import pytest
from sqlalchemy import func, select

from financeclaw.artifacts import S3ArtifactStore
from financeclaw.audit import AuditEventType, AuditRecord, SqlAlchemyAuditRepository
from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.observability import JsonLogFormatter, redact_sensitive
from financeclaw.outbox import OutboxPublisher, SqlAlchemyOutboxRepository
from financeclaw.outbox.tables import OutboxEventRow


def _audit_record() -> AuditRecord:
    return AuditRecord(
        event_type=AuditEventType.TOOL_ALLOWED,
        tenant_id="tenant-a",
        subject_id="subject-a",
        turn_id="turn-a",
        run_id="run-a",
        resource_type="tool",
        resource_id="market_snapshot",
        resource_version="1.0.0",
        action="READ",
        decision="allow",
        policy_version="stage5",
        payload_hash="0" * 64,
    )


@pytest.mark.asyncio
async def test_audit_and_outbox_are_atomic_and_publishable(tmp_path) -> None:
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'outbox.db'}")
    database.initialize_schema()
    audit = SqlAlchemyAuditRepository(database.session_factory)
    outbox = SqlAlchemyOutboxRepository(database.session_factory)
    audit.append(_audit_record())

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 1

    class Sink:
        def __init__(self) -> None:
            self.events = []

        async def publish(self, event) -> None:
            self.events.append(event)

    sink = Sink()
    assert await OutboxPublisher(outbox, sink).run_once() == 1
    assert sink.events[0].tenant_id == "tenant-a"
    with database.session_factory() as session:
        row = session.scalar(select(OutboxEventRow))
        assert row is not None
        assert row.status == "published"
        assert row.payload["payload_hash"] == "0" * 64
    database.close()


def test_s3_artifact_store_uses_scoped_key_hash_checksum_and_encryption() -> None:
    class Client:
        def __init__(self) -> None:
            self.put = None

        def put_object(self, **kwargs):
            self.put = kwargs

        def get_object(self, **_kwargs):
            return {"Body": io.BytesIO(b"report")}

        def head_bucket(self, **_kwargs):
            return {}

    client = Client()
    store = S3ArtifactStore(
        bucket="reports",
        prefix="production",
        sse_algorithm="aws:kms",
        kms_key_id="alias/financeclaw-artifacts",
        client=client,
    )

    uri = store.put(
        "artifact-123",
        b"report",
        tenant_id="sensitive-tenant-name",
        subject_id="sensitive-subject-name",
    )

    assert "sensitive-tenant-name" not in uri
    assert "sensitive-subject-name" not in uri
    assert client.put["ServerSideEncryption"] == "aws:kms"
    assert client.put["SSEKMSKeyId"] == "alias/financeclaw-artifacts"
    assert client.put["ChecksumSHA256"]
    assert store.get(uri) == b"report"
    assert store.health()


def test_structured_log_redaction_removes_credentials() -> None:
    import logging

    record = logging.LogRecord(
        "financeclaw.test",
        logging.INFO,
        __file__,
        1,
        "request Authorization: Bearer abc.def.ghi",
        (),
        None,
    )
    record.api_key = "deep-secret"
    payload = json.loads(JsonLogFormatter().format(record))

    assert "abc.def.ghi" not in payload["event"]
    assert payload["api_key"] == "[REDACTED]"
    assert redact_sensitive({"nested": {"password": "bad"}})["nested"]["password"] == "[REDACTED]"
