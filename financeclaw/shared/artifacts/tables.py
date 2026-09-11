"""制品元数据的 ORM 映射；与既有 artifact_metadata 表保持一致。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base, utcnow


class ArtifactMetadataRow(Base):
    """工件元数据表：记录外置工具结果等工件的存储与访问信息。

    使用场景：工具结果超出保留预算时正文外置到对象存储，本表保存其存储 URI、
    内容哈希、大小与访问/加密策略，供审计与按需回读。

    Attributes:
        artifact_id: 工件标识，主键（String(128)）。
        tenant_id: 租户标识（String(128)），非空，参与归属索引。
        subject_id: 主体标识（String(128)），非空，参与归属索引。
        content_type: 工件 MIME 类型（String(200)），非空。
        storage_uri: 对象存储 URI（Text），非空。
        content_hash: 内容 SHA-256 摘要（String(64)），非空。
        size_bytes: 内容字节数，非空。
        source_type: 产生工件的来源类型（String(64)），非空。
        source_id: 来源对象标识（String(128)），非空。
        access_policy: 访问策略（JSON 对象），非空，默认空对象。
        encryption_metadata: 加密元数据（JSON 对象），非空，默认空对象。
        created_at: 创建时间（带时区），非空，默认当前 UTC 时间。

    """

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_owner", "tenant_id", "subject_id", "artifact_id"),
        Index("ix_artifacts_turn", "tenant_id", "subject_id", "conversation_id", "source_turn_id"),
    )

    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(128))
    source_turn_id: Mapped[str | None] = mapped_column(String(128))
    source_run_id: Mapped[str | None] = mapped_column(String(128))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    encryption_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
