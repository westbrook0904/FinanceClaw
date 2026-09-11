"""The sole published graph factory; all specialist graphs remain internal tools."""

from functools import lru_cache

from financeclaw.shared.infrastructure.runtime import process_resources


@lru_cache(maxsize=1)
def _graph():
    """Compile the current published graph once, using the process resource set."""
    from financeclaw.agent_server.agents.offline import OfflineFinanceModel
    from financeclaw.agent_server.bootstrap import build_components

    resources = process_resources()
    components = build_components(resources=resources, enable_persistence=True)
    return components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel() if resources.settings.offline_model else None,
        checkpointer=None,
    )


async def finance_agent(config):
    """Native workers inject checkpointer/store; the API never invokes this graph directly."""
    return _graph()
