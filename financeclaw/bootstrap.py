"""Stage-1 composition root with immutable catalogs and no legacy Runtime."""

from dataclasses import dataclass

from financeclaw.agents import AgentFactory, AgentProfile, AgentProfileCatalog, ToolRef
from financeclaw.audit import AuditRepository, InMemoryAuditRepository
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.models import ModelFactory, ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.tools import ToolCatalog, ToolPolicy, default_local_tools, managed_mcp_quote_tool


@dataclass(frozen=True, slots=True)
class FinanceClawComponents:
    settings: FinanceClawSettings
    tool_catalog: ToolCatalog
    tool_policy: ToolPolicy
    audit: AuditRepository
    model_profiles: ModelProfileCatalog
    agent_profiles: AgentProfileCatalog
    model_factory: ModelFactory
    agent_factory: AgentFactory

    @property
    def default_agent_profile(self) -> AgentProfile:
        return self.agent_profiles.resolve("finance_agent", "1.0.0")


def build_components(
    settings: FinanceClawSettings | None = None,
    *,
    tool_catalog: ToolCatalog | None = None,
    audit: AuditRepository | None = None,
) -> FinanceClawComponents:
    settings = settings or FinanceClawSettings()
    if tool_catalog is None:
        tool_catalog = ToolCatalog(
            (
                *default_local_tools(),
                managed_mcp_quote_tool(timeout_seconds=settings.mcp_timeout_seconds),
            )
        )
    tool_policy = ToolPolicy()
    effective_audit = audit or InMemoryAuditRepository()

    fallback_profiles = tuple(
        ModelProfile(
            profile_id=f"fallback-{index}",
            version="1.0.0",
            model=model,
            temperature=0,
            timeout_seconds=settings.model_timeout_seconds,
            max_tokens=settings.model_max_tokens,
        )
        for index, model in enumerate(settings.fallback_models, start=1)
    )
    primary_profile = ModelProfile(
        profile_id="default",
        version="1.0.0",
        model=settings.model,
        temperature=0,
        timeout_seconds=settings.model_timeout_seconds,
        max_tokens=settings.model_max_tokens,
        fallback_profiles=tuple(
            ModelProfileRef(profile_id=profile.profile_id, version=profile.version)
            for profile in fallback_profiles
        ),
    )
    model_profiles = ModelProfileCatalog((primary_profile, *fallback_profiles))
    model_factory = ModelFactory(
        model_profiles,
        api_key=settings.provider_api_key,
        base_url=settings.provider_base_url,
    )
    agent_profile = AgentProfile(
        agent_id="finance_agent",
        version="1.0.0",
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        system_prompt_template=(
            "You are FinanceClaw's governed financial assistant. Use tools for current financial "
            "facts, preserve provider/as-of evidence, never invent tool results, never expose "
            "credentials, and never claim a WRITE occurred before approval and tool success."
        ),
        allowed_tools=tuple(
            ToolRef(tool_id=managed.governance.tool_id, version=managed.governance.version)
            for managed in tool_catalog.latest()
        ),
    )
    agent_profiles = AgentProfileCatalog((agent_profile,))
    agent_factory = AgentFactory(
        model_factory=model_factory,
        tool_catalog=tool_catalog,
        tool_policy=tool_policy,
        audit=effective_audit,
        debug_full_io=settings.debug_full_io,
        model_max_retries=settings.model_max_retries,
    )
    return FinanceClawComponents(
        settings=settings,
        tool_catalog=tool_catalog,
        tool_policy=tool_policy,
        audit=effective_audit,
        model_profiles=model_profiles,
        agent_profiles=agent_profiles,
        model_factory=model_factory,
        agent_factory=agent_factory,
    )
