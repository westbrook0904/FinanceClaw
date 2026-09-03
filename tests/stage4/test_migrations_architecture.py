"""`test_migrations_architecture` 模块提供`stage4`相关能力。"""

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from financeclaw.operations.workflow_eval_seed import SAMPLES

ROOT = Path(__file__).parents[2]


def test_stage4_migration_has_versioned_runs_approvals_and_idempotency(
    tmp_path: Path, monkeypatch
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 url，供后续步骤使用。
    url = f"sqlite+pysqlite:///{tmp_path / 'stage4-migration.db'}"
    # 前置条件满足后调用 setenv。
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    # 前置条件满足后调用 setenv。
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    # 前置条件满足后调用 upgrade。
    command.upgrade(Config("alembic.ini"), "head")
    # 准备 engine，供后续步骤使用。
    engine = create_engine(url)
    # 准备 inspector，供后续步骤使用。
    inspector = inspect(engine)
    # 继续执行前验证内部不变量。
    assert {"workflow_runs", "workflow_approvals", "delegations"} <= set(
        inspector.get_table_names()
    )
    # 准备 run_columns，供后续步骤使用。
    run_columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
    # 继续执行前验证内部不变量。
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
    # 准备 unique_constraints，供后续步骤使用。
    unique_constraints = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints("workflow_runs")
    }
    # 继续执行前验证内部不变量。
    assert unique_constraints["uq_workflow_runs_release_idempotency"] == (
        "tenant_id",
        "workflow_id",
        "workflow_version",
        "client_idempotency_key",
    )
    # 准备 approval_columns，供后续步骤使用。
    approval_columns = {column["name"] for column in inspector.get_columns("workflow_approvals")}
    # 继续执行前验证内部不变量。
    assert {
        "approval_point",
        "arguments_hash",
        "required_scope",
        "status",
        "decided_by",
        "expires_at",
    } <= approval_columns
    # 准备 delegation_columns，供后续步骤使用。
    delegation_columns = {column["name"] for column in inspector.get_columns("delegations")}
    # 继续执行前验证内部不变量。
    assert {
        "parent_run_id",
        "parent_turn_id",
        "child_run_id",
        "child_thread_id",
        "child_server_run_id",
        "kind",
        "target_id",
        "target_version",
        "request_fingerprint",
        "authorization_decision",
        "policy_version",
        "status",
        "delivered_at",
    } <= delegation_columns
    # 前置条件满足后调用 dispose。
    engine.dispose()


def test_only_code_published_workflows_are_registered_without_legacy_runtime() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    assert {sample["case"] for sample in SAMPLES} == {
        "normal_publication",
        "stale_snapshot_branch",
        "transient_tool_recovery",
        "approval_rejected",
        "checkpoint_resume",
    }
