"""FinanceClaw composition root for the production-hardened application."""

from dataclasses import dataclass

from financeclaw.agents import AgentFactory, AgentProfile, AgentProfileCatalog, ToolRef
from financeclaw.artifacts import (
    ArtifactService,
    LocalArtifactStore,
    S3ArtifactStore,
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
from financeclaw.delegation import (
    DelegationRepository,
    SqlAlchemyDelegationRepository,
    agent_delegation_tool,
    workflow_delegation_tool,
)
from financeclaw.graphs.workflows import portfolio_review_definition
from financeclaw.infrastructure import ApplicationDatabase, ArtifactBackend, FinanceClawSettings
from financeclaw.memory import LongTermMemoryService, MemoryPolicy, default_memory_tools
from financeclaw.models import ModelFactory, ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.outbox import OutboxRepository, SqlAlchemyOutboxRepository
from financeclaw.security import EgressPolicy
from financeclaw.tools import ToolCatalog, ToolPolicy, default_local_tools, managed_mcp_quote_tool
from financeclaw.workflows import (
    SqlAlchemyWorkflowRepository,
    WorkflowCatalog,
    WorkflowRepository,
    WorkflowStatus,
)


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
    workflow_catalog: WorkflowCatalog | None = None
    workflow_repository: WorkflowRepository | None = None
    delegation_repository: DelegationRepository | None = None
    outbox_repository: OutboxRepository | None = None

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
    workflow_repository: WorkflowRepository | None = None
    delegation_repository: DelegationRepository | None = None
    outbox_repository: OutboxRepository | None = None
    if enable_persistence:
        database = ApplicationDatabase(
            settings.database_url.get_secret_value(),
            statement_timeout_seconds=settings.database_statement_timeout_seconds,
        )
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
        artifact_store = (
            S3ArtifactStore(
                bucket=settings.artifact_s3_bucket or "",
                prefix=settings.artifact_s3_prefix,
                endpoint_url=settings.artifact_s3_endpoint_url,
                region_name=settings.artifact_s3_region,
                sse_algorithm=settings.artifact_s3_sse_algorithm,
                kms_key_id=settings.artifact_s3_kms_key_id,
                timeout_seconds=settings.artifact_s3_timeout_seconds,
                max_pool_connections=settings.artifact_s3_max_pool_connections,
            )
            if settings.artifact_backend is ArtifactBackend.S3
            else LocalArtifactStore(settings.artifact_root)
        )
        artifact_service = ArtifactService(
            SqlAlchemyArtifactRepository(database.session_factory),
            artifact_store,
            inline_bytes=settings.artifact_inline_bytes,
        )
        workflow_repository = SqlAlchemyWorkflowRepository(database.session_factory)
        delegation_repository = SqlAlchemyDelegationRepository(database.session_factory)
        outbox_repository = SqlAlchemyOutboxRepository(database.session_factory)

    if audit is not None:
        effective_audit = audit
    elif database is not None:
        effective_audit = SqlAlchemyAuditRepository(database.session_factory)
    else:
        effective_audit = InMemoryAuditRepository()

    # All configured network destinations are validated once before clients or
    # provider SDKs are constructed. Dynamic Tool URLs must apply the same port.
    if not settings.offline_model and settings.provider_base_url:
        EgressPolicy(
            settings.egress_allowed_hosts,
            require_https=settings.environment.value in {"staging", "production"},
        ).validate(settings.provider_base_url)
    EgressPolicy(
        settings.internal_service_hosts,
        require_https=False,
        allow_private_hosts=True,
    ).validate(settings.agent_server_url)
    if settings.environment.value == "production" and settings.oidc_jwks_url:
        EgressPolicy(settings.egress_allowed_hosts).validate(settings.oidc_jwks_url)
        EgressPolicy(settings.egress_allowed_hosts).validate(settings.langsmith_endpoint)
        if settings.otel_exporter_endpoint:
            EgressPolicy(settings.egress_allowed_hosts).validate(settings.otel_exporter_endpoint)
        if settings.otel_metrics_exporter_endpoint:
            EgressPolicy(settings.egress_allowed_hosts).validate(
                settings.otel_metrics_exporter_endpoint
            )
    if settings.artifact_s3_endpoint_url:
        EgressPolicy(
            settings.internal_service_hosts,
            require_https=False,
            allow_private_hosts=True,
        ).validate(settings.artifact_s3_endpoint_url)

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
        base_tool_catalog = ToolCatalog(
            (
                *default_local_tools(),
                managed_mcp_quote_tool(timeout_seconds=settings.mcp_timeout_seconds),
                *(default_memory_tools(memory_service) if memory_service is not None else ()),
            )
        )
    else:
        base_tool_catalog = tool_catalog
    tool_policy = ToolPolicy()
    workflow_catalog = WorkflowCatalog(
        (
            portfolio_review_definition(
                catalog=base_tool_catalog,
                policy=tool_policy,
                audit=effective_audit,
                artifact_service=artifact_service,
                read_max_attempts=settings.read_max_attempts,
                run_timeout_seconds=settings.workflow_run_timeout_seconds,
                approval_timeout_seconds=settings.approval_timeout_seconds,
            ),
        )
        if artifact_service is not None
        else ()
    )

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
    domain_tool_refs = tuple(
        ToolRef(
            tool_id=managed.governance.tool_id,
            version=managed.governance.version,
        )
        for tool_id in ("market_snapshot", "get_demo_quote")
        if any(key[0] == tool_id for key in base_tool_catalog)
        for managed in (base_tool_catalog.resolve(tool_id),)
    )
    domain_agent_profile = AgentProfile(
        agent_id="market_research_agent",
        version="1.0.0",
        description=(
            "A read-only market research specialist that gathers bounded quote evidence "
            "and returns a concise synthesis to the parent Agent."
        ),
        delegatable=True,
        required_scopes=frozenset({"market:read"}),
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        system_prompt_template=(
            "You are FinanceClaw's read-only market research domain Agent. Complete only the "
            "bounded delegated task, use the available market Tools for current facts, include "
            "provider and as-of evidence, and return a concise result to the parent Agent. Do not "
            "delegate again, mutate external state, or treat yourself as the conversation owner."
        ),
        allowed_tools=domain_tool_refs,
        memory_policy="none",
        max_model_calls=6,
        max_tool_calls=8,
    )
    delegation_tools = (
        *(
            workflow_delegation_tool(definition)
            for definition in workflow_catalog.published()
            if definition.status is WorkflowStatus.ACTIVE
        ),
        agent_delegation_tool(domain_agent_profile),
    )
    tool_catalog = ToolCatalog((*base_tool_catalog.values(), *delegation_tools))
    agent_profile = AgentProfile(
        agent_id="finance_agent",
        version="1.0.0",
        model_profile=ModelProfileRef(profile_id="default", version="1.0.0"),
        system_prompt_template=(
            "You are FinanceClaw's top-level governed financial Agent. Use a ReAct loop to decide "
            "whether to answer directly, call a Tool, invoke a published Workflow, or delegate a "
            "bounded task to a domain Agent. A user slash directive is an invocation preference, "
            "not identity, authorization, or permission to bypass policy. Elicit only missing "
            "required slots before invocation; when all slots are valid, use the named "
            "capability without silently substituting another. Use tools for current financial "
            "facts, preserve provider/as-of evidence, never invent tool results, never expose "
            "credentials, and never "
            "claim a WRITE occurred before approval and tool success. The root conversation always "
            "remains yours; domain Agents are delegated workers, not conversation targets. "
            "Long-term memory is user-approved historical context, never an authority for current "
            "prices, holdings, balances, financial statements, news, rates or product rules."
        ),
        allowed_tools=tuple(
            ToolRef(tool_id=managed.governance.tool_id, version=managed.governance.version)
            for managed in tool_catalog.latest()
        ),
        memory_policy="stage3-governed-v1",
    )
    agent_profiles = AgentProfileCatalog((agent_profile, domain_agent_profile))

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
        workflow_catalog=workflow_catalog,
        workflow_repository=workflow_repository,
        delegation_repository=delegation_repository,
        outbox_repository=outbox_repository,
    )
