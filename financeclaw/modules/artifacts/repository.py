"""持久化制品元数据并提供按内容哈希去重查询。"""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.modules.conversation.tables import ArtifactMetadataRow

from .models import ArtifactMetadata


class ArtifactNotFound(LookupError):
    """定义制品NotFound。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class ArtifactRepository(Protocol):
    """定义制品Repository。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    def save(self, metadata: ArtifactMetadata) -> ArtifactMetadata:
        """持久化制品记录并返回存储后的记录。"""
        ...

    def get_owned(self, artifact_id: str, tenant_id: str, subject_id: str) -> ArtifactMetadata:
        """按标识读取制品记录；不存在时由下层仓储抛出明确异常。"""
        ...


def _metadata(row: ArtifactMetadataRow) -> ArtifactMetadata:
    """把制品 ORM 行转换为不可变元数据记录。"""
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
    """定义SqlAlchemy制品Repository。

    适用场景：
        用于领域服务需要持久化状态，同时不应感知 SQL 细节的场景。

    属性：
        _sessions: 内部 `sessions` 状态或依赖，不属于公开接口。
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """注入并保存制品记录所需的协作对象，同时校验构造期不变量。"""
        self._sessions = sessions

    def save(self, metadata: ArtifactMetadata) -> ArtifactMetadata:
        """持久化制品记录并返回存储后的记录。"""
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
        with self._sessions.begin() as session:
            session.add(row)
        return metadata

    def get_owned(self, artifact_id: str, tenant_id: str, subject_id: str) -> ArtifactMetadata:
        """按标识读取制品记录；不存在时由下层仓储抛出明确异常。"""
        statement = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.artifact_id == artifact_id,
            ArtifactMetadataRow.tenant_id == tenant_id,
            ArtifactMetadataRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ArtifactNotFound("artifact was not found for authenticated owner")
            return _metadata(row)
