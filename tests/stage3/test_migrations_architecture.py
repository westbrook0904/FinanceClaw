"""`test_migrations_architecture` 模块提供`stage3`相关能力。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect

from financeclaw.application import TargetResolutionError, TargetResolver
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import ApplicationDatabase, FinanceClawSettings
from financeclaw.kernel import RunRequest, ToolTarget
from financeclaw.modules.audit import AuditEventType, SqlAlchemyAuditRepository
from financeclaw.modules.memory import MemoryDraft
from financeclaw.operations.memory_eval_seed import SAMPLES

from .support import conversation_context


def test_stage3_migration_adds_audit_and_manifest_memory_references(
    tmp_path: Path, monkeypatch
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 url，供后续步骤使用。
    url = f"sqlite+pysqlite:///{tmp_path / 'stage3-migration.db'}"
    # 前置条件满足后调用 setenv。
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    # 前置条件满足后调用 setenv。
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    # 准备 config，供后续步骤使用。
    config = Config("alembic.ini")
    # 前置条件满足后调用 upgrade。
    command.upgrade(config, "head")
    # 准备 engine，供后续步骤使用。
    engine = create_engine(url)
    # 准备 inspector，供后续步骤使用。
    inspector = inspect(engine)
    # 继续执行前验证内部不变量。
    assert "audit_records" in inspector.get_table_names()
    # 准备 manifest_columns，供后续步骤使用。
    manifest_columns = {
        column["name"] for column in inspector.get_columns("model_context_manifests")
    }
    # 继续执行前验证内部不变量。
    assert "memory_refs" in manifest_columns
    # 准备 audit_columns，供后续步骤使用。
    audit_columns = {column["name"] for column in inspector.get_columns("audit_records")}
    # 继续执行前验证内部不变量。
    assert {"tenant_id", "subject_id", "event_type", "payload_hash", "evidence_refs"} <= (
        audit_columns
    )
    # 前置条件满足后调用 dispose。
    engine.dispose()


def test_alembic_creates_missing_sqlite_parent_directory(tmp_path: Path, monkeypatch) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    database_path = tmp_path / "missing" / "nested" / "stage3.db"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", f"sqlite+pysqlite:///{database_path}")
    command.upgrade(Config("alembic.ini"), "head")
    assert database_path.is_file()


def test_stage3_runtime_has_no_legacy_memory_stack() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    root = Path(__file__).parents[2]
    sources = sorted((root / "financeclaw").rglob("*.py"))
    forbidden = ("harness_memory", "MemoryProvider", "MemoryGateway", "PRE_MEMORY")
    offenders = {
        str(path.relative_to(root)): term
        for path in sources
        for term in forbidden
        if term in path.read_text()
    }
    assert offenders == {}
    pyproject = (root / "pyproject.toml").read_text()
    assert '"harness_memory"' not in pyproject
    assert '"harness_policy"' not in pyproject


def test_langsmith_regression_seed_covers_required_memory_cases() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    assert {sample["case"] for sample in SAMPLES} == {
        "stable_preference_recall",
        "superseded_preference",
        "tenant_isolation",
        "current_tool_fact_wins",
        "high_impact_confirmation",
    }


def test_memory_tools_cannot_bypass_agent_human_approval_via_direct_target(
    tmp_path: Path,
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = build_components(
        FinanceClawSettings(
            environment="test",
            offline_model=True,
            debug_full_io=False,
            database_url=SecretStr(f"sqlite+pysqlite:///{tmp_path / 'direct.db'}"),
            artifact_root=str(tmp_path / "artifacts"),
        ),
        enable_persistence=True,
    )
    # 准备 resolver，供后续步骤使用。
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(TargetResolutionError, match="governed Agent path"):
        resolver.resolve(
            RunRequest(
                message="bypass memory approval",
                target=ToolTarget(
                    tool_id="confirm_memory",
                    arguments={
                        "proposal_id": "forged",
                        "kind": "preference",
                        "content": "forged",
                        "evidence_message_ids": ["current"],
                    },
                ),
            )
        )
    # 显式处理 `components.database is not None` 分支。
    if components.database is not None:
        components.database.close()


def test_memory_audit_survives_repository_reconstruction(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 path，供后续步骤使用。
    path = tmp_path / "audit.db"
    # 准备 settings，供后续步骤使用。
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=SecretStr(f"sqlite+pysqlite:///{path}"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    # 准备 components，供后续步骤使用。
    components = build_components(settings, enable_persistence=True)
    # 准备 repository，供后续步骤使用。
    repository = components.conversation_repository
    # 准备 service，供后续步骤使用。
    service = components.memory_service
    # 继续执行前验证内部不变量。
    assert repository is not None and service is not None
    # 准备 context and _，供后续步骤使用。
    context, _ = conversation_context(repository, key="persistent-audit")
    # 前置条件满足后调用 propose。
    service.propose(
        context,
        MemoryDraft(
            kind="goal",
            content="用户希望建立长期投资计划",
            evidence_message_ids=("current",),
        ),
    )
    # 继续执行前验证内部不变量。
    assert components.database is not None
    # 前置条件满足后调用 close。
    components.database.close()

    # 准备 database，供后续步骤使用。
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    # 准备 audit，供后续步骤使用。
    audit = SqlAlchemyAuditRepository(database.session_factory)
    # 准备 records，供后续步骤使用。
    records = audit.records(tenant_id=context.tenant_id, subject_id=context.subject_id)
    # 继续执行前验证内部不变量。
    assert [record.event_type for record in records] == [AuditEventType.MEMORY_PROPOSED]
    # 前置条件满足后调用 close。
    database.close()
