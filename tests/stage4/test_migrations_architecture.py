"""`test_migrations_architecture` 模块提供`stage4`相关能力。"""

import json
from pathlib import Path

from financeclaw.operations.workflow_eval_seed import SAMPLES

ROOT = Path(__file__).parents[2]


def test_only_code_published_workflows_are_registered_without_legacy_runtime() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    config = json.loads((ROOT / "langgraph.json").read_text())
    # HF-3 workflows are Tools inside the sole product root, not standalone HTTP graphs.
    assert set(config["graphs"]) == {"finance_agent_v1_6_0"}
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
