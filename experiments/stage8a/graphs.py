"""复用正式发布图，仅将市场研究的离线模型改为确定性地向用户提问。"""

import json
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.graphs.server_graphs import (
    components,
    finance_agent,
)
from financeclaw.agent_server.graphs.workflows.portfolio_review_v1 import (
    build_portfolio_review_graph,
)
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.local import MarketSnapshotTool


class QuestionModel(OfflineFinanceModel):
    """发布 Schema、治理工具和图都是真实实现；模型只生成合成问题。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """第一轮调用发布的提问工具，回答后沿正式结构化输出路径完成。"""
        if not isinstance(messages[-1], ToolMessage):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "request_user__research_scope",
                                    "args": {"question": "请确认合成测试研究区间。"},
                                    "id": "stage8a-user-question",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


market_research_agent = components.agent_factory.build(
    components.agent_profiles.resolve("market_research_agent", "1.2.0"),
    model=QuestionModel(),
    checkpointer=None,
)


class FreshSyntheticMarket(MarketSnapshotTool):
    """保持演示行情语义，显式合成当前时点以稳定覆盖审批路径。"""

    def _run(self, symbol):
        """不用真实市场资料，也不让固定演示日期令验收随日历失效。"""
        data = json.loads(super()._run(symbol))
        data.update(provider="stage8a-synthetic", as_of=datetime.now(UTC).isoformat())
        return json.dumps(data)


workflow_tools = ToolCatalog(
    [
        ManagedTool(tool=FreshSyntheticMarket(), governance=managed.governance)
        if managed.governance.tool_id == "market_snapshot"
        else managed
        for managed in components.tool_catalog.values()
    ]
)
portfolio_review_v1 = build_portfolio_review_graph(
    catalog=workflow_tools,
    policy=components.tool_policy,
    audit=components.audit,
    artifact_service=components.artifact_service,
    execution=components.conversation_repository.execution,
)
__all__ = ["finance_agent", "market_research_agent", "portfolio_review_v1"]
