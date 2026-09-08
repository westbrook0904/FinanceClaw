"""真实 Agent Server 验收图：生产 Factory、工具和中间件，仅模型为离线脚本。"""

import os
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.graphs.workflows.portfolio_review_v1 import (
    build_portfolio_review_graph,
)
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.stage6fixc.live_components import build_live_components


def server_components():
    """只使用验收临时目录，不读取用户 .env 或调用线上模型。"""
    return build_live_components(
        FinanceClawSettings(
            _env_file=None,
            environment="test",
            offline_model=True,
            database_url=SecretStr(os.environ["STAGE6FIX_TEST_DATABASE"]),
            artifact_root=os.environ["STAGE6FIX_TEST_ARTIFACTS"],
        ),
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
                    "task": "Read a bounded AAPL market snapshot "
                    + (
                        "live-child-interactions"
                        if "live-child-interactions" in str(last.content)
                        else ""
                    ),
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


class InteractiveChildModel(OfflineFinanceModel):
    """指定探针任务先资料、再选择、再原生审批，其余任务维持原离线行为。"""

    def _generate(self, messages, *args, **kwargs):
        """次序依赖真实工具回填，不以进程内状态模拟检查点。"""
        if not any(
            isinstance(message, HumanMessage) and "live-child-interactions" in str(message.content)
            for message in messages
        ):
            return super()._generate(messages, *args, **kwargs)
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == "watchlist_add":
            return super()._generate(messages, *args, **kwargs)
        if not isinstance(last, ToolMessage):
            name, arguments = (
                "request_user__research_scope",
                {"question": "请给出本次研究的时间区间。"},
            )
        elif last.name == "request_user__research_scope":
            name, arguments = (
                "request_user__research_focus",
                {"question": "请选择本次研究关注方向。"},
            )
        else:
            name, arguments = (
                "watchlist_add",
                {"symbol": "AAPL", "note": "isolated child approval test"},
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": arguments,
                                "id": "child-" + name,
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


components = server_components()
finance_agent = components.agent_factory.build(
    components.default_agent_profile, model=ChildThenApprovalModel(), checkpointer=None
)
market_research_agent = components.agent_factory.build(
    components.agent_profiles.resolve("market_research_agent"),
    model=InteractiveChildModel(),
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
