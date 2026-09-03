"""Artifact 元数据的仓储层实现。

位于 artifacts 模块的持久化边界：定义仓储协议并基于 SQLAlchemy 落库，表结构复用
conversation 模块的 ``ArtifactMetadataRow``，读写均按 tenant/subject 归属过滤。
"""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.modules.conversation.tables import ArtifactMetadataRow

from .models import ArtifactMetadata


class ArtifactNotFound(LookupError):
    """按归属查询 Artifact 元数据未命中时抛出的领域异常。

    使用场景：由仓储的 ``get_owned`` 在查无归属匹配的工件时抛出，服务层捕获后
    用于区分"工件不存在"与幂等复用等不同处理路径。
    """

    pass


class ArtifactRepository(Protocol):
    """Artifact 元数据仓储协议，约束持久化实现必须提供的读写能力。

    使用场景：由 ArtifactService 依赖注入使用，屏蔽具体存储实现；实现方按
    结构化子类型满足本协议即可，无需显式继承。
    """

    def save(self, metadata: ArtifactMetadata) -> ArtifactMetadata:
        """持久化一条 Artifact 元数据，成功后原样返回该元数据。"""
        ...

    def get_owned(self, artifact_id: str, tenant_id: str, subject_id: str) -> ArtifactMetadata:
        """按工件标识与归属租户/主体读取元数据，未命中时抛出 ArtifactNotFound。"""
        ...


def _metadata(row: ArtifactMetadataRow) -> ArtifactMetadata:
    """把 ORM 行 ``ArtifactMetadataRow`` 转换为领域模型 ``ArtifactMetadata``。"""
    return ArtifactMetadata(
        artifact_id=row.artifact_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        content_type=row.content_type,
        storage_uri=row.storage_uri,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        source_type=row.source_type,
        source_id=row.source_id,
        access_policy=row.access_policy,
        encryption_metadata=row.encryption_metadata,
        created_at=row.created_at,
    )


class SqlAlchemyArtifactRepository:
    """基于 SQLAlchemy 会话工厂的 Artifact 元数据仓储实现。

    使用场景：由基础设施层注入 ``sessionmaker``，在事务中完成元数据写入，并通过
    归属过滤查询支持 owner 隔离读取。

    Attributes:
        _sessions: SQLAlchemy 会话工厂，用于创建读写元数据的数据库会话（内部状态）。

    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """创建仓储实例。

        Args:
            sessions: 用于创建数据库会话的 SQLAlchemy 会话工厂。

        """
        self._sessions = sessions

    def save(self, metadata: ArtifactMetadata) -> ArtifactMetadata:
        """把一条 Artifact 元数据写入数据库，成功后返回原元数据。

        Args:
            metadata: 待持久化的工件元数据。

        Returns:
            原样返回的 ``metadata``，便于调用方继续使用。

        """
        # 1. 把领域模型映射为 ORM 行对象。
        row = ArtifactMetadataRow(
            artifact_id=metadata.artifact_id,
            tenant_id=metadata.tenant_id,
            subject_id=metadata.subject_id,
            content_type=metadata.content_type,
            storage_uri=metadata.storage_uri,
            content_hash=metadata.content_hash,
            size_bytes=metadata.size_bytes,
            source_type=metadata.source_type,
            source_id=metadata.source_id,
            access_policy=metadata.access_policy,
            encryption_metadata=metadata.encryption_metadata,
            created_at=metadata.created_at,
        )
        # 2. 在独立事务中落库，失败时整体回滚。
        with self._sessions.begin() as session:
            session.add(row)
        return metadata

    def get_owned(self, artifact_id: str, tenant_id: str, subject_id: str) -> ArtifactMetadata:
        """按工件标识与归属（租户/主体）查询元数据。

        Args:
            artifact_id: 工件唯一标识。
            tenant_id: 租户标识，参与归属过滤。
            subject_id: 主体标识，参与归属过滤。

        Returns:
            命中的工件元数据。

        Raises:
            ArtifactNotFound: 未找到归属匹配的工件元数据时抛出。

        """
        # 1. 构造同时匹配工件标识与归属三元组的查询。
        statement = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.artifact_id == artifact_id,
            ArtifactMetadataRow.tenant_id == tenant_id,
            ArtifactMetadataRow.subject_id == subject_id,
        )
        # 2. 执行查询，未命中即抛出异常，避免向非归属方泄露工件存在性。
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ArtifactNotFound("artifact was not found for authenticated owner")
            return _metadata(row)
