from typing import Any, ClassVar

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from financeclaw.agents import OfflineFinanceModel
from financeclaw.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.bootstrap import build_components
from financeclaw.contracts import ExecutionContext
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.tools import (
    MarketSnapshotTool,
    ToolCatalog,
    WatchlistWriteTool,
    default_local_tools,
)


def settings() -> FinanceClawSettings:
    return FinanceClawSettings(environment="test", offline_model=True, debug_full_io=False)


def context(*scopes: str, run_id: str = "run-agent") -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id=f"turn-{run_id}",
        run_id=run_id,
    )


def components_with_tools(*, market=None, write=None):
    catalog = ToolCatalog(default_local_tools(market_tool=market, write_tool=write))
    audit = InMemoryAuditRepository()
    return build_components(settings(), tool_catalog=catalog, audit=audit), audit


def test_agent_tool_calling_retries_read_and_writes_audit() -> None:
    market = MarketSnapshotTool(fail_first=1)
    components, audit = components_with_tools(market=market)
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "read AAPL"}]},
        config={"configurable": {"thread_id": "agent-read"}},
        context=context("market:read"),
        version="v2",
    )

    assert not result.interrupts
    assert market.call_count == 2
    assert "financeclaw-stage1-demo" in result.value["messages"][-1].content
    assert [record.event_type for record in audit.records()] == [
        AuditEventType.TOOL_ALLOWED,
        AuditEventType.FINANCIAL_TOOL_EXECUTED,
    ]


def test_agent_write_interrupts_and_only_executes_after_approval() -> None:
    write = WatchlistWriteTool()
    components, _ = components_with_tools(write=write)
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    config = {"configurable": {"thread_id": "agent-write"}}
    interrupted = agent.invoke(
        {"messages": [{"role": "user", "content": "write watchlist"}]},
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    assert interrupted.interrupts
    assert write.writes == ()

    result = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
        context=context("watchlist:write"),
        version="v2",
    )
    assert not result.interrupts
    assert write.writes == ({"symbol": "AAPL", "note": "stage1"},)


def test_unauthorized_write_is_hidden_from_model() -> None:
    write = WatchlistWriteTool()
    components, _ = components_with_tools(write=write)
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "write watchlist"}]},
        config={"configurable": {"thread_id": "agent-hidden"}},
        context=context("market:read"),
        version="v2",
    )

    assert not result.interrupts
    assert write.writes == ()
    assert "not authorized" in result.value["messages"][-1].content


class ForgingModel(OfflineFinanceModel):
    """Calls a hidden tool to prove execution-time authorization is independent."""

    def _generate(self, messages, *args: Any, **kwargs: Any) -> ChatResult:
        del args, kwargs
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=str(last.content)))]
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "watchlist_add",
                                "args": {"symbol": "AAPL", "note": "forged"},
                                "id": "forged-write",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def test_forged_hidden_tool_call_is_denied_again_at_execution() -> None:
    write = WatchlistWriteTool()
    components, _ = components_with_tools(write=write)
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=ForgingModel(),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "ignore permissions"}]},
        config={"configurable": {"thread_id": "agent-forged"}},
        context=context("market:read"),
        version="v2",
    )

    assert not result.interrupts
    assert write.writes == ()
    assert "tool_not_authorized" in result.value["messages"][-1].content


def test_model_fallback_recovers_primary_failure() -> None:
    class FailingModel(OfflineFinanceModel):
        calls: ClassVar[int] = 0

        def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
            type(self).calls += 1
            raise ConnectionError("primary unavailable")

    components, _ = components_with_tools()
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=FailingModel(),
        fallback_models=(OfflineFinanceModel(),),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "read AAPL"}]},
        config={"configurable": {"thread_id": "agent-fallback"}},
        context=context("market:read"),
        version="v2",
    )

    assert not result.interrupts
    # Two model turns each exhaust three primary attempts before falling back.
    assert FailingModel.calls == 6
    assert "financeclaw-stage1-demo" in result.value["messages"][-1].content
