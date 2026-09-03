"""Artifact（工件）领域模块的公开出口。

汇总大体积工具结果 offload 所需的元数据模型、仓储抽象、应用服务与存储后端实现，
供模块外部统一从本包导入。
"""

from .models import ArtifactMetadata
from .repository import ArtifactNotFound, ArtifactRepository, SqlAlchemyArtifactRepository
from .service import ArtifactService
from .storage import ArtifactStore, InMemoryArtifactStore, LocalArtifactStore, S3ArtifactStore

__all__ = [
    "ArtifactMetadata",
    "ArtifactNotFound",
    "ArtifactRepository",
    "ArtifactService",
    "ArtifactStore",
    "InMemoryArtifactStore",
    "LocalArtifactStore",
    "S3ArtifactStore",
    "SqlAlchemyArtifactRepository",
]
