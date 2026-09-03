"""Agent Server 对外暴露的 graph 注册入口。

langgraph.json 指向本模块：导入时即完成设置加载与组件装配，并把
顶层 Agent、领域 Agent、直连工具图与已发布固定流程暴露为模块级
助手，Agent Server 据此注册可运行的 graph。
"""

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.orchestration.agents import OfflineFinanceModel
from financeclaw.orchestration.graphs.direct_tool import build_direct_tool_graph

# Agent Server 共享的全局设置与装配后的组件集合（目录、策略、审计、制品服务等）。
settings = FinanceClawSettings()
components = build_components(settings, enable_persistence=True)

# 顶层金融 ReAct Agent 助手，面向会话编排工具调用、流程移交与领域委派。
finance_agent = components.agent_factory.build(
    components.default_agent_profile,
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
# 市场研究领域 Agent，承接行情检索类任务的专门委派。
market_research_agent = components.agent_factory.build(
    components.agent_profiles.resolve("market_research_agent", "1.0.0"),
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
# 直连工具图助手，承载 /tool <id> 的校验、授权、审批与执行链路。
direct_tool = build_direct_tool_graph(
    catalog=components.tool_catalog,
    policy=components.tool_policy,
    audit=components.audit,
    checkpointer=None,
    read_max_attempts=settings.read_max_attempts,
    artifact_service=components.artifact_service,
)
# 已发布工作流目录缺失即装配失败，避免带着不完整目录对外服务。
if components.workflow_catalog is None:
    raise RuntimeError("published workflow catalog was not configured")
# 首个固定流程助手 portfolio_review@1.0.0，注册后即可经 Agent Server 启动运行。
portfolio_review_v1 = components.workflow_catalog.resolve("portfolio_review", "1.0.0").graph
