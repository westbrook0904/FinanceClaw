import pytest
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, PrivateAttr

from financeclaw.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.contracts import ExecutionContext
from financeclaw.graphs.direct_tool import build_direct_tool_graph
from financeclaw.tools import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    ManagedTool,
    MarketSnapshotTool,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolCatalog,
    ToolGovernance,
    ToolPolicy,
    TransientToolError,
    WatchlistWriteTool,
    default_local_tools,
)


def context(*scopes: str) -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id="turn-a",
        run_id="run-a",
    )


def graph_for(*, market: MarketSnapshotTool | None = None, write: BaseTool | None = None):
    local = default_local_tools(market_tool=market, write_tool=write)  # type: ignore[arg-type]
    audit = InMemoryAuditRepository()
    graph = build_direct_tool_graph(
        catalog=ToolCatalog(local),
        policy=ToolPolicy(),
        audit=audit,
        checkpointer=InMemorySaver(),
        read_max_attempts=3,
    )
    return graph, audit, local


def test_direct_read_retries_then_projects_product_response() -> None:
    market = MarketSnapshotTool(fail_first=2)
    graph, audit, _ = graph_for(market=market)
    result = graph.invoke(
        {"tool_id": "market_snapshot", "version": None, "arguments": {"symbol": "AAPL"}},
        config={"configurable": {"thread_id": "read-retry"}},
        context=context("market:read"),
        version="v2",
    )

    assert result.value["response"]["status"] == "success"
    assert market.call_count == 3
    assert [record.event_type for record in audit.records()] == [
        AuditEventType.TOOL_ALLOWED,
        AuditEventType.FINANCIAL_TOOL_FAILED,
        AuditEventType.FINANCIAL_TOOL_FAILED,
        AuditEventType.FINANCIAL_TOOL_EXECUTED,
    ]


def test_direct_read_exhausts_bounded_retries_and_audits_each_failure() -> None:
    market = MarketSnapshotTool(fail_first=3)
    graph, audit, _ = graph_for(market=market)

    with pytest.raises(TransientToolError, match="temporarily unavailable"):
        graph.invoke(
            {"tool_id": "market_snapshot", "version": None, "arguments": {"symbol": "AAPL"}},
            config={"configurable": {"thread_id": "read-exhausted"}},
            context=context("market:read"),
            version="v2",
        )

    assert market.call_count == 3
    assert [record.event_type for record in audit.records()].count(
        AuditEventType.FINANCIAL_TOOL_FAILED
    ) == 3


def test_direct_read_validation_and_authorization_fail_closed() -> None:
    graph, _, _ = graph_for()
    invalid = graph.invoke(
        {"tool_id": "market_snapshot", "version": None, "arguments": {"symbol": "bad symbol"}},
        config={"configurable": {"thread_id": "read-invalid"}},
        context=context("market:read"),
        version="v2",
    )
    denied = graph.invoke(
        {"tool_id": "market_snapshot", "version": None, "arguments": {"symbol": "AAPL"}},
        config={"configurable": {"thread_id": "read-denied"}},
        context=context(),
        version="v2",
    )

    assert invalid.value["response"]["status"] == "failed"
    assert denied.value["response"]["status"] == "denied"


def test_write_interrupts_and_approve_or_reject_are_audited() -> None:
    write = WatchlistWriteTool()
    graph, audit, _ = graph_for(write=write)
    config = {"configurable": {"thread_id": "approve"}}
    interrupted = graph.invoke(
        {"tool_id": "watchlist_add", "version": None, "arguments": {"symbol": "AAPL"}},
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    assert interrupted.interrupts
    assert write.writes == ()

    approved = graph.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    assert approved.value["response"]["status"] == "success"
    assert write.writes == ({"symbol": "AAPL", "note": ""},)
    assert AuditEventType.TOOL_APPROVED in [record.event_type for record in audit.records()]

    rejected_config = {"configurable": {"thread_id": "reject"}}
    graph.invoke(
        {"tool_id": "watchlist_add", "version": None, "arguments": {"symbol": "MSFT"}},
        config=rejected_config,
        context=context("watchlist:write"),
        version="v2",
    )
    rejected = graph.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "not approved"}]}),
        config=rejected_config,
        context=context("watchlist:write"),
        version="v2",
    )
    assert rejected.value["response"]["status"] == "rejected"
    assert len(write.writes) == 1


def test_edited_arguments_invalidate_approval_and_require_a_new_one() -> None:
    write = WatchlistWriteTool()
    graph, _, _ = graph_for(write=write)
    config = {"configurable": {"thread_id": "edit"}}
    first = graph.invoke(
        {
            "tool_id": "watchlist_add",
            "version": None,
            "arguments": {"symbol": "AAPL", "note": "old"},
        },
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    first_hash = first.interrupts[0].value["arguments_hash"]
    edited = graph.invoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "watchlist_add",
                            "args": {"symbol": "MSFT", "note": "new"},
                        },
                    }
                ]
            }
        ),
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    second_hash = edited.interrupts[0].value["arguments_hash"]

    assert first_hash != second_hash
    assert write.writes == ()
    approved = graph.invoke(
        Command(resume={"decisions": [{"type": "approve", "arguments_hash": second_hash}]}),
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    assert approved.value["response"]["status"] == "success"
    assert write.writes == ({"symbol": "MSFT", "note": "new"},)


class EmptyInput(BaseModel):
    pass


class FailingWriteTool(BaseTool):
    name: str = "failing_write"
    description: str = "Always fails to prove WRITE is not retried."
    args_schema: type[BaseModel] = EmptyInput
    _calls: int = PrivateAttr(default=0)

    @property
    def calls(self) -> int:
        return self._calls

    def _run(self) -> str:
        self._calls += 1
        raise TransientToolError("do not retry a write")


def test_write_failure_is_never_retried_and_is_audited() -> None:
    tool = FailingWriteTool()
    managed = ManagedTool(
        tool,
        ToolGovernance(
            tool_id=tool.name,
            version="1.0.0",
            side_effect=SideEffect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            risk_level=RiskLevel.HIGH,
            required_scopes=frozenset({"write"}),
            approval=ApprovalMode.ALWAYS,
            egress=Egress.INTERNAL,
            sensitivity=Sensitivity.CONFIDENTIAL,
            retry_profile=RetryProfile.NONE,
            audit_level=AuditLevel.FULL,
        ),
    )
    audit = InMemoryAuditRepository()
    graph = build_direct_tool_graph(
        catalog=ToolCatalog((managed,)),
        policy=ToolPolicy(),
        audit=audit,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "no-write-retry"}}
    graph.invoke(
        {"tool_id": tool.name, "version": None, "arguments": {}},
        config=config,
        context=context("write"),
        version="v2",
    )
    with pytest.raises(TransientToolError, match="do not retry"):
        graph.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=context("write"),
            version="v2",
        )
    assert tool.calls == 1
    assert [record.event_type for record in audit.records()].count(
        AuditEventType.FINANCIAL_TOOL_FAILED
    ) == 1
