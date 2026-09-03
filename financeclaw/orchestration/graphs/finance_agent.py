"""向 LangGraph Server 暴露配置驱动的顶层 Agent 与直接工具图。"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.orchestration.agents import OfflineFinanceModel
from financeclaw.orchestration.graphs.direct_tool import build_direct_tool_graph


def make_finance_agent(config: RunnableConfig) -> Any:
    """从运行配置组装并返回 LangGraph Server 使用的顶层金融 Agent。"""
    del config
    settings = FinanceClawSettings()
    components = build_components(settings, enable_persistence=True)
    model = OfflineFinanceModel() if settings.offline_model else None
    return components.agent_factory.build(
        components.default_agent_profile,
        model=model,
        checkpointer=None,
    )


def make_direct_tool_graph(config: RunnableConfig) -> Any:
    """从运行配置组装并返回带治理和审批的直接工具图。"""
    del config
    settings = FinanceClawSettings()
    components = build_components(settings, enable_persistence=True)
    return build_direct_tool_graph(
        catalog=components.tool_catalog,
        policy=components.tool_policy,
        audit=components.audit,
        checkpointer=None,
        read_max_attempts=settings.read_max_attempts,
        artifact_service=components.artifact_service,
    )
