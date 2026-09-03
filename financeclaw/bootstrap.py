"""FinanceClaw composition root for the governed Stage-3 vertical slice."""

from dataclasses import dataclass

from financeclaw.agents import AgentFactory, AgentProfile, AgentProfileCatalog, ToolRef
from financeclaw.artifacts import (
    ArtifactService,
    LocalArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.audit import (
    AuditRepository,
    InMemoryAuditRepository,
    SqlAlchemyAuditRepository,
)
from financeclaw.conversation import (
    ContextBudget,
    ConversationContextBuilder,
    ConversationRepository,
    SqlAlchemyConversationRepository,
    SummaryService,
)
from financeclaw.infrastructure import ApplicationDatabase, FinanceClawSettings
from financeclaw.memory import LongTermMemoryService, MemoryPolicy, default_memory_tools
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
    database: ApplicationDatabase | None = None
    conversation_repository: ConversationRepository | None = None
    context_builder: ConversationContextBuilder | None = None
    summary_service: SummaryService | None = None
    artifact_service: ArtifactService | None = None
    memory_service: LongTermMemoryService | None = None

    @property
    def default_agent_profile(self) -> AgentProfile:
        return self.agent_profiles.resolve("finance_agent", "1.0.0")


def build_components(
    settings: FinanceClawSettings | None = None,
    *,
    tool_catalog: ToolCatalog | None = None,
    audit: AuditRepository | None = None,
    enable_persistence: bool = False,
) -> FinanceClawComponents:
    settings = settings or FinanceClawSettings()

    # Application persistence is composed before catalogs because Stage-3
    # memory tools need Journal evidence and durable Audit dependencies. The
    # memory records themselves remain in the request-scoped LangGraph Store.
    database: ApplicationDatabase | None = None
    conversation_repository: ConversationRepository | None = None
    context_builder: ConversationContextBuilder | None = None
    summary_service: SummaryService | None = None
    artifact_service: ArtifactService | None = None
    if enable_persistence:
        database = ApplicationDatabase(settings.database_url.get_secret_value())
        if settings.database_auto_create_schema:
            database.initialize_schema()
        concrete_repository = SqlAlchemyConversationRepository(database.session_factory)
        conversation_repository = concrete_repository
        context_builder = ConversationContextBuilder(
            concrete_repository,
            ContextBudget(
                model_input_limit=settings.context_input_limit,
                reserved_output_tokens=settings.context_reserved_output,
                system_policy_reserve=settings.context_system_policy_reserve,
                tool_schema_reserve=settings.context_tool_schema_reserve,
                safety_margin=settings.context_safety_margin,
            ),
        )
        summary_service = SummaryService(
            concrete_repository,
            segment_messages=settings.summary_segment_messages,
            hierarchy_segments=settings.summary_hierarchy_segments,
        )
        artifact_service = ArtifactService(
            SqlAlchemyArtifactRepository(database.session_factory),
            LocalArtifactStore(settings.artifact_root),
            inline_bytes=settings.artifact_inline_bytes,
        )

    if audit is not None:
        effective_audit = audit
    elif database is not None:
        effective_audit = SqlAlchemyAuditRepository(database.session_factory)
    else:
        effective_audit = InMemoryAuditRepository()

    memory_service = (
        LongTermMemoryService(
            conversation_repository=conversation_repository,
            audit=effective_audit,
            policy=MemoryPolicy(
                auto_commit_low_risk_preferences=(settings.memory_auto_commit_low_risk_preferences)
            ),
        )
        if conversation_repository is not None
        else None
    )
    if tool_catalog is None:
        tool_catalog = ToolCatalog(
            (
                *default_local_tools(),
                managed_mcp_quote_tool(timeout_seconds=settings.mcp_timeout_seconds),
                *(default_memory_tools(memory_service) if memory_service is not None else ()),
            )
        )
    tool_policy = ToolPolicy()

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
            "credentials, and never claim a WRITE occurred before approval and tool success. "
            "Long-term memory is user-approved historical context, never an authority for current "
            "prices, holdings, balances, financial statements, news, rates or product rules."
        ),
        allowed_tools=tuple(
            ToolRef(tool_id=managed.governance.tool_id, version=managed.governance.version)
            for managed in tool_catalog.latest()
        ),
        memory_policy="stage3-governed-v1",
    )
    agent_profiles = AgentProfileCatalog((agent_profile,))

    agent_factory = AgentFactory(
        model_factory=model_factory,
        tool_catalog=tool_catalog,
        tool_policy=tool_policy,
        audit=effective_audit,
        debug_full_io=settings.debug_full_io,
        model_max_retries=settings.model_max_retries,
        context_builder=context_builder,
        conversation_repository=conversation_repository,
        artifact_service=artifact_service,
        memory_service=memory_service,
        memory_recall_tokens=settings.memory_recall_tokens,
        memory_recall_limit=settings.memory_recall_limit,
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
        database=database,
        conversation_repository=conversation_repository,
        context_builder=context_builder,
        summary_service=summary_service,
        artifact_service=artifact_service,
        memory_service=memory_service,
    )
