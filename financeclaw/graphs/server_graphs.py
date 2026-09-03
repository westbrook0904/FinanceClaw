"""Process-lifetime graph instances loaded once by LangGraph Agent Server."""

from financeclaw.agents import OfflineFinanceModel
from financeclaw.bootstrap import build_components
from financeclaw.graphs.direct_tool import build_direct_tool_graph
from financeclaw.infrastructure import FinanceClawSettings

settings = FinanceClawSettings()
components = build_components(settings, enable_persistence=True)

finance_agent = components.agent_factory.build(
    components.default_agent_profile,
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
