"""Agent Server graph factories with Stage-2 persistent context support."""

from typing import Any

from langchain_core.runnables import RunnableConfig

from financeclaw.agents import OfflineFinanceModel
from financeclaw.bootstrap import build_components
from financeclaw.graphs.direct_tool import build_direct_tool_graph
from financeclaw.infrastructure import FinanceClawSettings


def make_finance_agent(config: RunnableConfig) -> Any:
    """Construct the default finance_agent; Agent Server owns checkpointing."""

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
    """Construct the deterministic direct_tool graph for Agent Server."""

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
