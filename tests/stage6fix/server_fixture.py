"""真实 Agent Server 验收图：生产 Factory、工具和中间件，仅模型为离线脚本。"""

import os
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.orchestration.agents import OfflineFinanceModel
from financeclaw.orchestration.graphs.workflows.portfolio_review_v1 import (
    build_portfolio_review_graph,
)


def server_components():
    """只使用验收临时目录，不读取用户 .env 或调用线上模型。"""
    return build_components(
        FinanceClawSettings(
            _env_file=None,
            environment="test",
            offline_model=True,
            database_url=SecretStr(os.environ["STAGE6FIX_TEST_DATABASE"]),
            artifact_root=os.environ["STAGE6FIX_TEST_ARTIFACTS"],
        ),
        enable_persistence=True,
    )


class ChildThenApprovalModel(OfflineFinanceModel):
    """真实 ReAct：领域委派完成后才请求写动作，批准或拒绝后生成最终回复。"""

    def _generate(self, messages, *args, **kwargs):
        """由模型脚本固定工具次序，工具执行与 interrupt 均交给真实框架。"""
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == "watchlist_add":
            result = AIMessage(content="Final: " + str(last.content))
        else:
            name = (
                "watchlist_add"
                if isinstance(last, ToolMessage)
                else "delegate_agent__market_research_agent"
            )
            arguments = (
                {"symbol": "AAPL", "note": "stage6fix real server"}
                if isinstance(last, ToolMessage)
                else {
                    "task": "Read a bounded AAPL market snapshot",
                    "arguments": {"symbols": ["AAPL"]},
                }
            )
            result = AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": arguments, "id": "live-" + name, "type": "tool_call"}
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=result)])


components = server_components()
finance_agent = components.agent_factory.build(
    components.default_agent_profile, model=ChildThenApprovalModel(), checkpointer=None
)
market_research_agent = components.agent_factory.build(
    components.agent_profiles.resolve("market_research_agent"),
    model=OfflineFinanceModel(),
    checkpointer=None,
)
# 演示行情时点固定在 2026-09-02；仅在测试图中固定新鲜度时钟，避免用例随日期失效。
# 审批期限仍由 BFF 的真实时钟处理，框架、执行预算和发布节点均使用生产实现。
portfolio_review_v1 = build_portfolio_review_graph(
    catalog=components.tool_catalog,
    policy=components.tool_policy,
    audit=components.audit,
    artifact_service=components.artifact_service,
    execution=components.conversation_repository.execution,
    read_max_attempts=components.settings.read_max_attempts,
    clock=lambda: datetime(2026, 9, 2, 2, tzinfo=UTC),
)
