"""Artifact metadata ownership repository."""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.conversation.tables import ArtifactMetadataRow

from .models import ArtifactMetadata


class ArtifactNotFound(LookupError):
    pass


class ArtifactRepository(Protocol):
    def save(self, metadata: ArtifactMetadata) -> ArtifactMetadata: ...

    def get_owned(self, artifact_id: str, tenant_id: str, subject_id: str) -> ArtifactMetadata: ...


def _metadata(row: ArtifactMetadataRow) -> ArtifactMetadata:
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
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def save(self, metadata: ArtifactMetadata) -> ArtifactMetadata:
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
