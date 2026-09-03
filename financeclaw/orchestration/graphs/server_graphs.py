"""声明 LangGraph Server 可发现的图工厂导出。"""

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.orchestration.agents import OfflineFinanceModel
from financeclaw.orchestration.graphs.direct_tool import build_direct_tool_graph

settings = FinanceClawSettings()
components = build_components(settings, enable_persistence=True)

finance_agent = components.agent_factory.build(
    components.default_agent_profile,
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
market_research_agent = components.agent_factory.build(
    components.agent_profiles.resolve("market_research_agent", "1.0.0"),
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
direct_tool = build_direct_tool_graph(
    catalog=components.tool_catalog,
    policy=components.tool_policy,
    audit=components.audit,
    checkpointer=None,
    read_max_attempts=settings.read_max_attempts,
    artifact_service=components.artifact_service,
)
if components.workflow_catalog is None:
    raise RuntimeError("published workflow catalog was not configured")
portfolio_review_v1 = components.workflow_catalog.resolve("portfolio_review", "1.0.0").graph
