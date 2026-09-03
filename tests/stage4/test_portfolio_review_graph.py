"""`test_portfolio_review_graph` 模块提供`stage4`相关能力。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from financeclaw.infrastructure import ApplicationDatabase
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.artifacts import (
    ArtifactService,
    InMemoryArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.modules.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.modules.workflows import WorkflowCatalog
from financeclaw.orchestration.graphs.workflows import (
    PortfolioReviewInput,
    portfolio_review_definition,
)
from financeclaw.orchestration.tools import (
    MarketSnapshotTool,
    ToolCatalog,
    ToolPolicy,
    default_local_tools,
)


def _context(*scopes: str, run_id: str = "run-workflow") -> ExecutionContext:
    """处理 `当前操作`，并返回边界约定的结果。"""
    return ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id="workflow-turn",
        run_id=run_id,
    )


def _input(*, max_age: int = 48) -> dict:
    """处理 `当前操作`，并返回边界约定的结果。"""
    return {
        "portfolio_name": "Core portfolio",
        "positions": [
            {"symbol": "AAPL", "quantity": "2", "cost_basis": "80"},
            {"symbol": "MSFT", "quantity": "1", "cost_basis": "90"},
        ],
        "max_snapshot_age_hours": max_age,
    }


def _workflow(tmp_path: Path, *, market: MarketSnapshotTool | None = None):
    """处理 `当前操作`，并返回边界约定的结果。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and _ and _ and definition，供后续步骤使用。
    database, _, _, definition = _workflow(tmp_path)
    # 准备 catalog，供后续步骤使用。
    catalog = WorkflowCatalog((definition,))
    # 继续执行前验证内部不变量。
    assert catalog.resolve("portfolio_review").version == "1.0.0"
    # 继续执行前验证内部不变量。
    assert definition.assistant_id == "portfolio_review_v1"
    # 继续执行前验证内部不变量。
    assert [(tool.tool_id, tool.version) for tool in definition.allowed_tools] == [
        ("market_snapshot", "1.0.0")
    ]
    # 继续执行前验证内部不变量。
    assert [point.approval_id for point in definition.approval_points] == [
        "publish_portfolio_report"
    ]
    # 继续执行前验证内部不变量。
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
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(TypeError):
        catalog[("portfolio_review", "9.9.9")] = definition  # type: ignore[index]
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ValidationError):
        PortfolioReviewInput.model_validate({**_input(), "runtime_node": "unsafe"})
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
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
    # 前置条件满足后调用 close。
    database.close()


def test_graph_interrupts_then_publishes_one_bounded_provenance_artifact(
    tmp_path: Path,
) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and store and audit and
    # definition，供后续步骤使用。
    database, store, audit, definition = _workflow(tmp_path)
    # 准备 config，供后续步骤使用。
    config = {"configurable": {"thread_id": "portfolio-approve"}}
    # 准备 context，供后续步骤使用。
    context = _context("portfolio:review", "market:read", "workflows:approve")
    # 准备 interrupted，供后续步骤使用。
    interrupted = definition.graph.invoke(_input(), config=config, context=context, version="v2")
    # 准备 approval，供后续步骤使用。
    approval = interrupted.interrupts[0].value
    # 继续执行前验证内部不变量。
    assert approval["approval_point"] == "publish_portfolio_report"
    # 继续执行前验证内部不变量。
    assert approval["workflow_version"] == "1.0.0"
    # 继续执行前验证内部不变量。
    assert approval["allowed_decisions"] == ["approve", "reject"]
    # 继续执行前验证内部不变量。
    assert store.values == {}

    # 准备 completed，供后续步骤使用。
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
    # 准备 output，供后续步骤使用。
    output = completed.value
    # 继续执行前验证内部不变量。
    assert output["status"] == "completed"
    # 继续执行前验证内部不变量。
    assert output["total_market_value"] == "300.00"
    # 继续执行前验证内部不变量。
    assert output["largest_position_weight"] == "0.6667"
    # 继续执行前验证内部不变量。
    assert output["risk_band"] == "high"
    # 继续执行前验证内部不变量。
    assert {item["provider"] for item in output["source_refs"]} == {"financeclaw-stage1-demo"}
    # 准备 artifact_id，供后续步骤使用。
    artifact_id = output["artifact"]["artifact_id"]
    # 继续执行前验证内部不变量。
    assert len(store.values) == 1
    # 继续执行前验证内部不变量。
    assert next(iter(store.values)).endswith(f"/{artifact_id}")

    # 准备 state，供后续步骤使用。
    state = definition.graph.get_state(config).values
    # 准备 serialized_state，供后续步骤使用。
    serialized_state = repr(state).lower()
    # 继续执行前验证内部不变量。
    assert "api_key" not in serialized_state
    # 继续执行前验证内部不变量。
    assert "credential" not in serialized_state
    # 继续执行前验证内部不变量。
    assert "disclaimer" not in serialized_state
    # 准备 events，供后续步骤使用。
    events = [record.event_type for record in audit.records()]
    # 继续执行前验证内部不变量。
    assert events.count(AuditEventType.FINANCIAL_TOOL_EXECUTED) == 2
    # 前置条件满足后调用 close。
    database.close()


def test_reject_stale_authorization_and_transient_retry_fail_closed(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 market，供后续步骤使用。
    market = MarketSnapshotTool(fail_first=1)
    # 准备 database and store and audit and
    # definition，供后续步骤使用。
    database, store, audit, definition = _workflow(tmp_path, market=market)
    # 准备 context，供后续步骤使用。
    context = _context("portfolio:review", "market:read", "workflows:approve")
    # 准备 config，供后续步骤使用。
    config = {"configurable": {"thread_id": "portfolio-reject"}}
    # 准备 interrupted，供后续步骤使用。
    interrupted = definition.graph.invoke(
        {
            "portfolio_name": "retry",
            "positions": [{"symbol": "AAPL", "quantity": 1, "cost_basis": 80}],
        },
        config=config,
        context=context,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert interrupted.interrupts
    # 继续执行前验证内部不变量。
    assert market.call_count == 2
    # 准备 rejected，供后续步骤使用。
    rejected = definition.graph.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "not now"}]}),
        config=config,
        context=context,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert rejected.value["status"] == "rejected"
    # 继续执行前验证内部不变量。
    assert store.values == {}
    # 继续执行前验证内部不变量。
    assert AuditEventType.FINANCIAL_TOOL_FAILED in [record.event_type for record in audit.records()]

    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(PermissionError):
        definition.graph.invoke(
            _input(),
            config={"configurable": {"thread_id": "portfolio-denied"}},
            context=_context("portfolio:review"),
            version="v2",
        )

    # 准备 stale_definition，供后续步骤使用。
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
    # 准备 stale，供后续步骤使用。
    stale = stale_definition.graph.invoke(
        _input(max_age=24),
        config={"configurable": {"thread_id": "portfolio-stale"}},
        context=context,
        version="v2",
    )
    # 继续执行前验证内部不变量。
    assert stale.value["status"] == "failed"
    # 继续执行前验证内部不变量。
    assert "older" in stale.value["error"]
    # 前置条件满足后调用 close。
    database.close()


def test_report_artifact_write_is_idempotent_for_the_same_run_node(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database，供后续步骤使用。
    database = ApplicationDatabase(f"sqlite+pysqlite:///{tmp_path / 'artifact-once.db'}")
    # 前置条件满足后调用 initialize schema。
    database.initialize_schema()
    # 准备 store，供后续步骤使用。
    store = InMemoryArtifactStore()
    # 准备 service，供后续步骤使用。
    service = ArtifactService(SqlAlchemyArtifactRepository(database.session_factory), store)
    # 准备 context，供后续步骤使用。
    context = _context("artifacts:read", run_id="run-idempotent")
    # 准备 first，供后续步骤使用。
    first = service.persist(
        {"report": "stable"},
        context=context,
        source_type="workflow_report",
        source_id="portfolio_review@1.0.0",
        idempotency_key="run-idempotent:publish:v1",
    )
    # 准备 replay，供后续步骤使用。
    replay = service.persist(
        {"report": "stable"},
        context=context,
        source_type="workflow_report",
        source_id="portfolio_review@1.0.0",
        idempotency_key="run-idempotent:publish:v1",
    )
    # 继续执行前验证内部不变量。
    assert replay.artifact_id == first.artifact_id
    # 继续执行前验证内部不变量。
    assert replay.content_hash == first.content_hash
    # 继续执行前验证内部不变量。
    assert len(store.values) == 1
    # 继续执行前验证内部不变量。
    assert next(iter(store.values)).endswith(f"/{first.artifact_id}")
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ValueError, match="different content"):
        service.persist(
            {"report": "changed"},
            context=context,
            source_type="workflow_report",
            source_id="portfolio_review@1.0.0",
            idempotency_key="run-idempotent:publish:v1",
        )
    # 前置条件满足后调用 close。
    database.close()
