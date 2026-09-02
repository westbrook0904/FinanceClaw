from pathlib import Path

import pytest

from financeclaw.artifacts import (
    ArtifactService,
    InMemoryArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.contracts import ExecutionContext
from financeclaw.infrastructure import ApplicationDatabase


def context(*scopes: str, tenant_id: str = "tenant-a") -> ExecutionContext:
    return ExecutionContext(
        tenant_id=tenant_id,
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id="turn-artifact",
        run_id="run-artifact",
    )


def test_large_result_is_offloaded_hashed_and_owner_scoped(tmp_path: Path) -> None:
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'artifact.db'}")
    database.initialize_schema()
    repository = SqlAlchemyArtifactRepository(database.session_factory)
    store = InMemoryArtifactStore()
    service = ArtifactService(repository, store, inline_bytes=256)
    original = {
        "rows": ["market-data" * 100],
        "provider": "test-market-provider",
        "as_of": "2026-09-02T00:00:00Z",
    }

    projected, metadata = service.offload(
        original,
        context=context("artifacts:read"),
        source_type="tool_result",
        source_id="large-market-query",
    )

    assert metadata is not None
    assert metadata.size_bytes > 256
    assert metadata.artifact_id in projected
    assert "test-market-provider" in projected
    assert "2026-09-02T00:00:00Z" in projected
    assert (
        "market-data"
        in service.read(metadata.artifact_id, context=context("artifacts:read")).decode()
    )
    with pytest.raises(PermissionError):
        service.read(metadata.artifact_id, context=context())
    with pytest.raises(LookupError):
        service.read(
            metadata.artifact_id,
            context=context("artifacts:read", tenant_id="tenant-b"),
        )
    database.close()
