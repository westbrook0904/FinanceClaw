"""`test_artifacts` 模块提供`stage2`相关能力。"""

from pathlib import Path

import pytest

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.artifacts import (
    ArtifactService,
    InMemoryArtifactStore,
    SqlAlchemyArtifactRepository,
)


def context(*scopes: str, tenant_id: str = "tenant-a") -> ExecutionContext:
    """处理 `当前操作`，并返回边界约定的结果。"""
    return ExecutionContext(
        tenant_id=tenant_id,
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id="turn-artifact",
        run_id="run-artifact",
    )


def test_large_result_is_offloaded_hashed_and_owner_scoped(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database，供后续步骤使用。
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'artifact.db'}")
    # 前置条件满足后调用 initialize schema。
    database.initialize_schema()
    # 准备 repository，供后续步骤使用。
    repository = SqlAlchemyArtifactRepository(database.session_factory)
    # 准备 store，供后续步骤使用。
    store = InMemoryArtifactStore()
    # 准备 service，供后续步骤使用。
    service = ArtifactService(repository, store, inline_bytes=256)
    # 准备 original，供后续步骤使用。
    original = {
        "rows": ["market-data" * 100],
        "provider": "test-market-provider",
        "as_of": "2026-09-02T00:00:00Z",
    }

    # 准备 projected and metadata，供后续步骤使用。
    projected, metadata = service.offload(
        original,
        context=context("artifacts:read"),
        source_type="tool_result",
        source_id="large-market-query",
    )

    # 继续执行前验证内部不变量。
    assert metadata is not None
    # 继续执行前验证内部不变量。
    assert metadata.size_bytes > 256
    # 继续执行前验证内部不变量。
    assert metadata.artifact_id in projected
    # 继续执行前验证内部不变量。
    assert "test-market-provider" in projected
    # 继续执行前验证内部不变量。
    assert "2026-09-02T00:00:00Z" in projected
    # 继续执行前验证内部不变量。
    assert (
        "market-data"
        in service.read(metadata.artifact_id, context=context("artifacts:read")).decode()
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(PermissionError):
        service.read(metadata.artifact_id, context=context())
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(LookupError):
        service.read(
            metadata.artifact_id,
            context=context("artifacts:read", tenant_id="tenant-b"),
        )
    # 前置条件满足后调用 close。
    database.close()
