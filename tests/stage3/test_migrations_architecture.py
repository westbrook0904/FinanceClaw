from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect

from financeclaw.application import TargetResolutionError, TargetResolver
from financeclaw.application.memory_eval_seed import SAMPLES
from financeclaw.audit import AuditEventType, SqlAlchemyAuditRepository
from financeclaw.bootstrap import build_components
from financeclaw.contracts import RunRequest, ToolTarget
from financeclaw.infrastructure import ApplicationDatabase, FinanceClawSettings
from financeclaw.memory import MemoryDraft

from .support import conversation_context


def test_stage3_migration_adds_audit_and_manifest_memory_references(
    tmp_path: Path, monkeypatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'stage3-migration.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "audit_records" in inspector.get_table_names()
    manifest_columns = {
        column["name"] for column in inspector.get_columns("model_context_manifests")
    }
    assert "memory_refs" in manifest_columns
    audit_columns = {column["name"] for column in inspector.get_columns("audit_records")}
    assert {"tenant_id", "subject_id", "event_type", "payload_hash", "evidence_refs"} <= (
        audit_columns
    )
    engine.dispose()


def test_alembic_creates_missing_sqlite_parent_directory(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "missing" / "nested" / "stage3.db"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", f"sqlite+pysqlite:///{database_path}")
    command.upgrade(Config("alembic.ini"), "head")
    assert database_path.is_file()


def test_stage3_runtime_has_no_legacy_memory_stack() -> None:
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
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
    )
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
    if components.database is not None:
        components.database.close()


def test_memory_audit_survives_repository_reconstruction(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=SecretStr(f"sqlite+pysqlite:///{path}"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    components = build_components(settings, enable_persistence=True)
    repository = components.conversation_repository
    service = components.memory_service
    assert repository is not None and service is not None
    context, _ = conversation_context(repository, key="persistent-audit")
    service.propose(
        context,
        MemoryDraft(
            kind="goal",
            content="用户希望建立长期投资计划",
            evidence_message_ids=("current",),
        ),
    )
    assert components.database is not None
    components.database.close()

    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    audit = SqlAlchemyAuditRepository(database.session_factory)
    records = audit.records(tenant_id=context.tenant_id, subject_id=context.subject_id)
    assert [record.event_type for record in records] == [AuditEventType.MEMORY_PROPOSED]
    database.close()
