from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agents import (
    InvocationKind,
    OfflineFinanceModel,
    parse_invocation_directive,
)
from financeclaw.bootstrap import build_components
from financeclaw.contracts import ExecutionContext
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.tools import MarketSnapshotTool, ToolCatalog, default_local_tools


def _settings() -> FinanceClawSettings:
    return FinanceClawSettings(environment="test", offline_model=True, debug_full_io=False)


def _context(*scopes: str, run_id: str) -> ExecutionContext:
    return ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id=f"turn-{run_id}",
        run_id=run_id,
    )


def test_slash_directive_is_parsed_as_an_untrusted_invocation_preference() -> None:
    directive = parse_invocation_directive(
        '/tool calculate {"operation":"subtract","left":7,"right":2}'
    )

    assert directive is not None
    assert directive.kind is InvocationKind.TOOL
    assert directive.resource_id == "calculate"
    assert directive.arguments == {"operation": "subtract", "left": 7, "right": 2}


def test_complete_tool_directive_calls_only_the_named_tool_with_validated_arguments() -> None:
    components = build_components(_settings())
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": ('/tool calculate {"operation":"subtract","left":7,"right":2}'),
                }
            ]
        },
        config={"configurable": {"thread_id": "directive-complete"}},
        context=_context("tools:read", run_id="directive-complete"),
        version="v2",
    )

    tool_messages = [
        message for message in result.value["messages"] if isinstance(message, ToolMessage)
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "calculate"
    assert '"value": "5.0"' in str(tool_messages[0].content)


def test_missing_required_slots_elicits_without_executing_the_tool() -> None:
    market = MarketSnapshotTool()
    catalog = ToolCatalog(default_local_tools(market_tool=market))
    components = build_components(_settings(), tool_catalog=catalog)
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "/tool market_snapshot"}]},
        config={"configurable": {"thread_id": "directive-missing"}},
        context=_context("market:read", run_id="directive-missing"),
        version="v2",
    )

    assert market.call_count == 0
    assert "provide" in result.value["messages"][-1].content.lower()


def test_incomplete_directive_is_denied_again_if_the_model_invents_arguments() -> None:
    class InventingModel(OfflineFinanceModel):
        def _generate(self, messages, *args: Any, **kwargs: Any) -> ChatResult:
            if isinstance(messages[-1], ToolMessage):
                return super()._generate(messages, *args, **kwargs)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "market_snapshot",
                                    "args": {"symbol": "INVENTED"},
                                    "id": "invented-slot-call",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

    market = MarketSnapshotTool()
    components = build_components(
        _settings(),
        tool_catalog=ToolCatalog(default_local_tools(market_tool=market)),
    )
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=InventingModel(),
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "/tool market_snapshot"}]},
        config={"configurable": {"thread_id": "directive-forged-slot"}},
        context=_context("market:read", run_id="directive-forged-slot"),
        version="v2",
    )

    assert market.call_count == 0
    assert "tool_not_authorized" in result.value["messages"][-1].content


def test_workflow_directive_does_not_turn_into_a_public_target_or_tool_substitution() -> None:
    components = build_components(_settings())
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "/workflow portfolio_review account A-001"}]},
        config={"configurable": {"thread_id": "directive-workflow"}},
        context=_context("market:read", run_id="directive-workflow"),
        version="v2",
    )

    assert "no registered delegation capability" in result.value["messages"][-1].content
