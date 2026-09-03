import tomllib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from financeclaw.evaluation import EvaluationResult, RegressionGate, load_cases

ROOT = Path(__file__).resolve().parents[2]


def test_versioned_regression_dataset_covers_every_stage5_gate() -> None:
    cases = load_cases(ROOT / "evals" / "stage5-regression-v1.json")
    results = tuple(
        EvaluationResult(case_id=case.case_id, passed=True, score=1.0) for case in cases
    )

    RegressionGate(minimum_score=0.95).assert_passed(cases, results)


def test_stage5_migration_adds_transactional_outbox(tmp_path, monkeypatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'stage5.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)

    command.upgrade(Config("alembic.ini"), "head")

    engine = create_engine(url)
    assert "outbox_events" in inspect(engine).get_table_names()
    indexes = {item["name"] for item in inspect(engine).get_indexes("outbox_events")}
    assert {"ix_outbox_delivery", "ix_outbox_owner"}.issubset(indexes)
    engine.dispose()


def test_sbom_script_and_ci_release_controls_are_committed() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "uv sync --frozen" in ci
    assert "pip-audit --strict --require-hashes --disable-pip" in ci
    assert "uv export --frozen --no-dev --no-emit-project" in ci
    assert "generate_sbom.py" in ci
    assert "check_secret_leaks.py" in ci
    assert "uv sync --frozen --no-dev" in dockerfile


def test_agent_server_runtime_is_not_in_the_default_bff_dependency_set() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    default_dependencies = "\n".join(project["project"]["dependencies"])

    assert "langgraph-cli" not in default_dependencies
    assert "langgraph-checkpoint-postgres" not in default_dependencies
    assert "langgraph-checkpoint-redis" not in default_dependencies
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    assert any("langgraph-cli" in item for item in dev_dependencies)
