import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from financeclaw.application.workflow_eval_seed import SAMPLES

ROOT = Path(__file__).parents[2]


def test_stage4_migration_has_versioned_runs_approvals_and_idempotency(
    tmp_path: Path, monkeypatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'stage4-migration.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert {"workflow_runs", "workflow_approvals"} <= set(inspector.get_table_names())
    run_columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
    assert {
        "workflow_id",
        "workflow_version",
        "assistant_id",
        "deployment_revision",
        "thread_id",
        "server_run_id",
        "arguments_hash",
        "artifact_refs",
    } <= run_columns
    unique_constraints = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints("workflow_runs")
    }
    assert unique_constraints["uq_workflow_runs_release_idempotency"] == (
        "tenant_id",
        "workflow_id",
        "workflow_version",
        "client_idempotency_key",
    )
    approval_columns = {column["name"] for column in inspector.get_columns("workflow_approvals")}
    assert {
        "approval_point",
        "arguments_hash",
        "required_scope",
        "status",
        "decided_by",
        "expires_at",
    } <= approval_columns
    engine.dispose()


def test_only_code_published_workflows_are_registered_without_legacy_runtime() -> None:
    config = json.loads((ROOT / "langgraph.json").read_text())
    assert config["graphs"]["portfolio_review_v1"].endswith(":portfolio_review_v1")
    production_source = "\n".join(
        path.read_text() for path in sorted((ROOT / "financeclaw").rglob("*.py"))
    )
    forbidden = (
        "PlanDraft",
        "ExecutionPlan",
        "DAGBuilder",
        "NodeProvider",
        "harness_planning",
        "harness_execution",
        "harness_runtime",
    )
    assert {term for term in forbidden if term in production_source} == set()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert all(f'"{package}"' not in pyproject for package in forbidden[-3:])


def test_langsmith_seed_covers_every_required_workflow_path() -> None:
    assert {sample["case"] for sample in SAMPLES} == {
        "normal_publication",
        "stale_snapshot_branch",
        "transient_tool_recovery",
        "approval_rejected",
        "checkpoint_resume",
    }
