from datetime import UTC, datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from financeclaw.artifacts import (
    ArtifactService,
    InMemoryArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.contracts import ExecutionContext
from financeclaw.graphs.workflows import (
    PortfolioReviewInput,
    portfolio_review_definition,
)
from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.tools import MarketSnapshotTool, ToolCatalog, ToolPolicy, default_local_tools
from financeclaw.workflows import WorkflowCatalog


def _context(*scopes: str, run_id: str = "run-workflow") -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id="workflow-turn",
        run_id=run_id,
    )


def _input(*, max_age: int = 48) -> dict:
    return {
        "portfolio_name": "Core portfolio",
        "positions": [
            {"symbol": "AAPL", "quantity": "2", "cost_basis": "80"},
            {"symbol": "MSFT", "quantity": "1", "cost_basis": "90"},
        ],
        "max_snapshot_age_hours": max_age,
    }


def _workflow(tmp_path: Path, *, market: MarketSnapshotTool | None = None):
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'workflow.db'}")
    database.initialize_schema()
    store = InMemoryArtifactStore()
    artifact_service = ArtifactService(
        SqlAlchemyArtifactRepository(database.session_factory), store
    )
    audit = InMemoryAuditRepository()
    tool_catalog = ToolCatalog(default_local_tools(market_tool=market))
    definition = portfolio_review_definition(
        catalog=tool_catalog,
        policy=ToolPolicy(),
        audit=audit,
        artifact_service=artifact_service,
        checkpointer=InMemorySaver(),
        clock=lambda: datetime(2026, 9, 3, tzinfo=UTC),
    )
    return database, store, audit, definition


def test_catalog_schema_and_topology_are_immutable_and_version_pinned(tmp_path: Path) -> None:
    database, _, _, definition = _workflow(tmp_path)
    catalog = WorkflowCatalog((definition,))
    assert catalog.resolve("portfolio_review").version == "1.0.0"
    assert definition.assistant_id == "portfolio_review_v1"
    assert [(tool.tool_id, tool.version) for tool in definition.allowed_tools] == [
        ("market_snapshot", "1.0.0")
    ]
    assert [point.approval_id for point in definition.approval_points] == [
        "publish_portfolio_report"
    ]
    assert set(definition.graph.get_graph().nodes) == {
        "__start__",
        "normalize_input",
        "load_market_snapshots",
        "validate_freshness",
        "analyze_exposure",
        "publication_approval",
        "publish_report",
        "finalize",
        "__end__",
    }
    with pytest.raises(TypeError):
        catalog[("portfolio_review", "9.9.9")] = definition  # type: ignore[index]
    with pytest.raises(ValidationError):
        PortfolioReviewInput.model_validate({**_input(), "runtime_node": "unsafe"})
    with pytest.raises(ValidationError, match="unique symbols"):
        PortfolioReviewInput.model_validate(
            {
                "portfolio_name": "duplicate",
                "positions": [
                    {"symbol": "AAPL", "quantity": 1, "cost_basis": 1},
                    {"symbol": "aapl", "quantity": 1, "cost_basis": 1},
                ],
            }
        )
    database.close()


def test_graph_interrupts_then_publishes_one_bounded_provenance_artifact(
    tmp_path: Path,
) -> None:
    database, store, audit, definition = _workflow(tmp_path)
    config = {"configurable": {"thread_id": "portfolio-approve"}}
    context = _context("portfolio:review", "market:read", "workflows:approve")
    interrupted = definition.graph.invoke(_input(), config=config, context=context, version="v2")
    approval = interrupted.interrupts[0].value
    assert approval["approval_point"] == "publish_portfolio_report"
    assert approval["workflow_version"] == "1.0.0"
    assert approval["allowed_decisions"] == ["approve", "reject"]
    assert store.values == {}

    completed = definition.graph.invoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "approve",
                        "arguments_hash": approval["arguments_hash"],
                    }
                ]
            }
        ),
        config=config,
        context=context,
        version="v2",
    )
    output = completed.value
    assert output["status"] == "completed"
    assert output["total_market_value"] == "300.00"
    assert output["largest_position_weight"] == "0.6667"
    assert output["risk_band"] == "high"
    assert {item["provider"] for item in output["source_refs"]} == {"financeclaw-stage1-demo"}
    artifact_id = output["artifact"]["artifact_id"]
    assert list(store.values) == [artifact_id]

    state = definition.graph.get_state(config).values
    serialized_state = repr(state).lower()
    assert "api_key" not in serialized_state
    assert "credential" not in serialized_state
    assert "disclaimer" not in serialized_state
    events = [record.event_type for record in audit.records()]
    assert events.count(AuditEventType.FINANCIAL_TOOL_EXECUTED) == 2
    database.close()


def test_reject_stale_authorization_and_transient_retry_fail_closed(tmp_path: Path) -> None:
    market = MarketSnapshotTool(fail_first=1)
    database, store, audit, definition = _workflow(tmp_path, market=market)
    context = _context("portfolio:review", "market:read", "workflows:approve")
    config = {"configurable": {"thread_id": "portfolio-reject"}}
    interrupted = definition.graph.invoke(
        {
            "portfolio_name": "retry",
            "positions": [{"symbol": "AAPL", "quantity": 1, "cost_basis": 80}],
        },
        config=config,
        context=context,
        version="v2",
    )
    assert interrupted.interrupts
    assert market.call_count == 2
    rejected = definition.graph.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "not now"}]}),
        config=config,
        context=context,
        version="v2",
    )
    assert rejected.value["status"] == "rejected"
    assert store.values == {}
    assert AuditEventType.FINANCIAL_TOOL_FAILED in [record.event_type for record in audit.records()]

    with pytest.raises(PermissionError):
        definition.graph.invoke(
            _input(),
            config={"configurable": {"thread_id": "portfolio-denied"}},
            context=_context("portfolio:review"),
            version="v2",
        )

    stale_definition = portfolio_review_definition(
        catalog=ToolCatalog(default_local_tools()),
        policy=ToolPolicy(),
        audit=InMemoryAuditRepository(),
        artifact_service=ArtifactService(
            SqlAlchemyArtifactRepository(database.session_factory), store
        ),
        checkpointer=InMemorySaver(),
        clock=lambda: datetime(2026, 9, 10, tzinfo=UTC),
    )
    stale = stale_definition.graph.invoke(
        _input(max_age=24),
        config={"configurable": {"thread_id": "portfolio-stale"}},
        context=context,
        version="v2",
    )
    assert stale.value["status"] == "failed"
    assert "older" in stale.value["error"]
    database.close()


def test_report_artifact_write_is_idempotent_for_the_same_run_node(tmp_path: Path) -> None:
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'artifact-once.db'}")
    database.initialize_schema()
    store = InMemoryArtifactStore()
    service = ArtifactService(SqlAlchemyArtifactRepository(database.session_factory), store)
    context = _context("artifacts:read", run_id="run-idempotent")
    first = service.persist(
        {"report": "stable"},
        context=context,
        source_type="workflow_report",
        source_id="portfolio_review@1.0.0",
        idempotency_key="run-idempotent:publish:v1",
    )
    replay = service.persist(
        {"report": "stable"},
        context=context,
        source_type="workflow_report",
        source_id="portfolio_review@1.0.0",
        idempotency_key="run-idempotent:publish:v1",
    )
    assert replay.artifact_id == first.artifact_id
    assert replay.content_hash == first.content_hash
    assert list(store.values) == [first.artifact_id]
    with pytest.raises(ValueError, match="different content"):
        service.persist(
            {"report": "changed"},
            context=context,
            source_type="workflow_report",
            source_id="portfolio_review@1.0.0",
            idempotency_key="run-idempotent:publish:v1",
        )
    database.close()
