"""Product Agent Server exports one root graph; all Workers remain internal Tools."""

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.shared.infrastructure.observability.langsmith import configure_langsmith
from financeclaw.shared.infrastructure.settings import FinanceClawSettings

settings = FinanceClawSettings()
configure_langsmith(
    project=settings.langsmith_project,
    endpoint=settings.langsmith_endpoint,
    sample_rate=settings.langsmith_trace_sample_rate,
    hide_inputs=settings.langsmith_hide_inputs,
    hide_outputs=settings.langsmith_hide_outputs,
)
components = build_components(settings, enable_persistence=True)
finance_agent = components.agent_factory.build(
    components.agent_profiles.resolve("finance_agent", "1.5.0"),
    model=OfflineFinanceModel() if settings.offline_model else None,
    checkpointer=None,
)
