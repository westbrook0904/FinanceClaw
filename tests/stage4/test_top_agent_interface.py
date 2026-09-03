"""`test_top_agent_interface` 模块提供`stage4`相关能力。"""

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.kernel import ExecutionContext
from financeclaw.orchestration.agents import (
    InvocationKind,
    OfflineFinanceModel,
    parse_invocation_directive,
)
from financeclaw.orchestration.tools import MarketSnapshotTool, ToolCatalog, default_local_tools


def _settings() -> FinanceClawSettings:
    """处理 `当前操作`，并返回边界约定的结果。"""
    return FinanceClawSettings(environment="test", offline_model=True, debug_full_io=False)


def _context(*scopes: str, run_id: str) -> ExecutionContext:
    """处理 `当前操作`，并返回边界约定的结果。"""
    return ExecutionContext(
        tenant_id="tenant-a",
        subject_id="subject-a",
        scopes=frozenset(scopes),
        turn_id=f"turn-{run_id}",
        run_id=run_id,
    )


def test_slash_directive_is_parsed_as_an_untrusted_invocation_preference() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    directive = parse_invocation_directive(
        '/tool calculate {"operation":"subtract","left":7,"right":2}'
    )

    assert directive is not None
    assert directive.kind is InvocationKind.TOOL
    assert directive.resource_id == "calculate"
    assert directive.arguments == {"operation": "subtract", "left": 7, "right": 2}


def test_complete_tool_directive_calls_only_the_named_tool_with_validated_arguments() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 components，供后续步骤使用。
    components = build_components(_settings())
    # 准备 agent，供后续步骤使用。
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )

    # 准备 result，供后续步骤使用。
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

    # 准备 tool_messages，供后续步骤使用。
    tool_messages = [
        message for message in result.value["messages"] if isinstance(message, ToolMessage)
    ]
    # 继续执行前验证内部不变量。
    assert len(tool_messages) == 1
    # 继续执行前验证内部不变量。
    assert tool_messages[0].name == "calculate"
    # 继续执行前验证内部不变量。
    assert '"value": "5.0"' in str(tool_messages[0].content)


def test_missing_required_slots_elicits_without_executing_the_tool() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
    """验证函数名所描述的业务场景符合预期。"""

    # 定义当前操作使用的局部 InventingModel 辅助类型。
    class InventingModel(OfflineFinanceModel):
        """`InventingModel` 封装该模块内聚的状态与行为。"""

        def _generate(self, messages, *args: Any, **kwargs: Any) -> ChatResult:
            """处理 `InventingModel`，并返回边界约定的结果。"""
            # 显式处理 `isinstance(messages[-1],
            # ToolMessage)` 分支。
            if isinstance(messages[-1], ToolMessage):
                return super()._generate(messages, *args, **kwargs)
            # 向调用方返回符合边界约定的结果。
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

    # 准备 market，供后续步骤使用。
    market = MarketSnapshotTool()
    # 准备 components，供后续步骤使用。
    components = build_components(
        _settings(),
        tool_catalog=ToolCatalog(default_local_tools(market_tool=market)),
    )
    # 准备 agent，供后续步骤使用。
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=InventingModel(),
    )

    # 准备 result，供后续步骤使用。
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "/tool market_snapshot"}]},
        config={"configurable": {"thread_id": "directive-forged-slot"}},
        context=_context("market:read", run_id="directive-forged-slot"),
        version="v2",
    )

    # 继续执行前验证内部不变量。
    assert market.call_count == 0
    # 继续执行前验证内部不变量。
    assert "tool_not_authorized" in result.value["messages"][-1].content


def test_workflow_directive_does_not_turn_into_a_public_target_or_tool_substitution() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
