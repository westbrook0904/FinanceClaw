"""`test_outbox_artifact_observability` 模块提供`stage5`相关能力。"""

import io
import json

import pytest
from sqlalchemy import func, select

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.infrastructure.observability import JsonLogFormatter, redact_sensitive
from financeclaw.modules.artifacts import S3ArtifactStore
from financeclaw.modules.audit import AuditEventType, AuditRecord, SqlAlchemyAuditRepository
from financeclaw.modules.outbox import OutboxPublisher, SqlAlchemyOutboxRepository
from financeclaw.modules.outbox.tables import OutboxEventRow


def _audit_record() -> AuditRecord:
    """处理 `record`，并返回边界约定的结果。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database，供后续步骤使用。
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'outbox.db'}")
    # 前置条件满足后调用 initialize schema。
    database.initialize_schema()
    # 准备 audit，供后续步骤使用。
    audit = SqlAlchemyAuditRepository(database.session_factory)
    # 准备 outbox，供后续步骤使用。
    outbox = SqlAlchemyOutboxRepository(database.session_factory)
    # 前置条件满足后调用 append。
    audit.append(_audit_record())

    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) == 1

    # 定义当前操作使用的局部 Sink 辅助类型。
    class Sink:
        """`Sink` 封装该模块内聚的状态与行为。"""

        def __init__(self) -> None:
            """初始化 `Sink` 及其必需的协作对象。"""
            self.events = []

        async def publish(self, event) -> None:
            """发布 `Sink`，并返回边界约定的结果。"""
            self.events.append(event)

    # 准备 sink，供后续步骤使用。
    sink = Sink()
    # 继续执行前验证内部不变量。
    assert await OutboxPublisher(outbox, sink).run_once() == 1
    # 继续执行前验证内部不变量。
    assert sink.events[0].tenant_id == "tenant-a"
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with database.session_factory() as session:
        row = session.scalar(select(OutboxEventRow))
        assert row is not None
        assert row.status == "published"
        assert row.payload["payload_hash"] == "0" * 64
    # 前置条件满足后调用 close。
    database.close()


def test_s3_artifact_store_uses_scoped_key_hash_checksum_and_encryption() -> None:
    """验证函数名所描述的业务场景符合预期。"""

    # 定义当前操作使用的局部 Client 辅助类型。
    class Client:
        """`Client` 封装外部服务的调用边界。"""

        def __init__(self) -> None:
            """初始化 `Client` 及其必需的协作对象。"""
            self.put = None

        def put_object(self, **kwargs):
            """存储 `object`，并返回边界约定的结果。"""
            self.put = kwargs

        def get_object(self, **_kwargs):
            """获取 `object`，并返回边界约定的结果。"""
            return {"Body": io.BytesIO(b"report")}

        def head_bucket(self, **_kwargs):
            """处理 `bucket`，并返回边界约定的结果。"""
            return {}

    # 准备 client，供后续步骤使用。
    client = Client()
    # 准备 store，供后续步骤使用。
    store = S3ArtifactStore(
        bucket="reports",
        prefix="production",
        sse_algorithm="aws:kms",
        kms_key_id="alias/financeclaw-artifacts",
        client=client,
    )

    # 准备 uri，供后续步骤使用。
    uri = store.put(
        "artifact-123",
        b"report",
        tenant_id="sensitive-tenant-name",
        subject_id="sensitive-subject-name",
    )

    # 继续执行前验证内部不变量。
    assert "sensitive-tenant-name" not in uri
    # 继续执行前验证内部不变量。
    assert "sensitive-subject-name" not in uri
    # 继续执行前验证内部不变量。
    assert client.put["ServerSideEncryption"] == "aws:kms"
    # 继续执行前验证内部不变量。
    assert client.put["SSEKMSKeyId"] == "alias/financeclaw-artifacts"
    # 继续执行前验证内部不变量。
    assert client.put["ChecksumSHA256"]
    # 继续执行前验证内部不变量。
    assert store.get(uri) == b"report"
    # 继续执行前验证内部不变量。
    assert store.health()


def test_structured_log_redaction_removes_credentials() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
