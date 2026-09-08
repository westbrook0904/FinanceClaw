"""Agent Server 对外暴露的 graph 注册入口。

langgraph.json 指向本模块：导入时即完成设置加载与组件装配，并把
顶层 Agent、领域 Agent、直连工具图与已发布固定流程暴露为模块级
助手，Agent Server 据此注册可运行的 graph。
"""

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.graphs.direct_tool import build_direct_tool_graph
from financeclaw.agent_server.graphs.ziwei_agent import build_ziwei_agent
from financeclaw.shared.infrastructure.observability.langsmith import configure_langsmith
from financeclaw.shared.infrastructure.settings import FinanceClawSettings

# Agent Server 共享的全局设置与装配后的组件集合（目录、策略、审计、制品服务等）。
settings = FinanceClawSettings()
configure_langsmith(
    project=settings.langsmith_project,
    endpoint=settings.langsmith_endpoint,
    sample_rate=settings.langsmith_trace_sample_rate,
    hide_inputs=settings.langsmith_hide_inputs,
    hide_outputs=settings.langsmith_hide_outputs,
)
components = build_components(settings, enable_persistence=True, enable_subgraphs=True)

# 顶层金融 ReAct Agent 助手，面向会话编排工具调用、流程移交与领域委派。
finance_agent = components.agent_factory.build(
    components.agent_profiles.resolve("finance_agent", "1.4.0"),
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
# 市场研究领域 Agent，承接行情检索类任务的专门委派。
market_research_agent = components.agent_factory.build(
    components.agent_profiles.resolve("market_research_agent", "1.2.0"),
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
# 当前紫微领域图；关闭候选时由 preflight 返回 unsupported。
ziwei_doushu_agent_text = build_ziwei_agent(
    components.agent_factory,
    components.agent_profiles.resolve("ziwei_doushu_agent", "2.0.0"),
    components.ziwei_service,
    model=OfflineZiweiModel() if settings.offline_model else None,
    input_budget=min(24_000, settings.context_input_limit - settings.context_reserved_output),
)
# 内部直连工具图助手；产品消息中的 /tool 仍先进入顶层 Agent 的指令中间件。
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

# HF-1 candidate; BFF admission remains pinned to 1.4.0 until HF-2 cutover.
finance_agent_subgraphs = components.agent_factory.build(
    components.agent_profiles.resolve("finance_agent", "1.5.0"),
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
