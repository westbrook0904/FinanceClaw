"""组合根（composition root）：把业务模块的 Port 与具体基础设施实现装配起来。

本模块集中选择数据库、对象存储、模型与 Agent 的实现，产出
``FinanceClawComponents``。HTTP 的 ``create_default_app`` 继续装配客户端、
认证器、应用服务和生命周期；Agent Server 的 graph 入口负责加载组件并注册图。
当前仓储实现随领域模块组织，不能把这个组合根理解为全项目已实现纯 Port 依赖。
"""

from dataclasses import dataclass

from financeclaw.application.ziwei_service import ZiweiService
from financeclaw.infrastructure import ApplicationDatabase, ArtifactBackend, FinanceClawSettings
from financeclaw.infrastructure.llm import (
    ModelFactory,
    ModelProfile,
    ModelProfileCatalog,
    ModelProfileRef,
)
from financeclaw.infrastructure.security import EgressPolicy
from financeclaw.modules.artifacts import (
    ArtifactService,
    LocalArtifactStore,
    S3ArtifactStore,
    SqlAlchemyArtifactRepository,
)
from financeclaw.modules.audit import (
    AuditRepository,
    InMemoryAuditRepository,
    SqlAlchemyAuditRepository,
)
from financeclaw.modules.conversation import (
    ContextBudget,
    ConversationContextBuilder,
    ConversationRepository,
    SqlAlchemyConversationRepository,
    SummaryService,
)
from financeclaw.modules.delegation import (
    DelegationRepository,
    SqlAlchemyDelegationRepository,
)
from financeclaw.modules.memory import LongTermMemoryService, MemoryPolicy
from financeclaw.modules.outbox import OutboxRepository, SqlAlchemyOutboxRepository
from financeclaw.modules.workflows import (
    SqlAlchemyWorkflowRepository,
    WorkflowCatalog,
    WorkflowRepository,
    WorkflowStatus,
)
from financeclaw.orchestration.agents import (
    AgentFactory,
    AgentProfile,
    AgentProfileCatalog,
    ToolRef,
)
from financeclaw.orchestration.graphs.workflows import portfolio_review_definition
from financeclaw.orchestration.tools import (
    ToolCatalog,
    ToolPolicy,
    default_local_tools,
    managed_mcp_quote_tool,
)
from financeclaw.orchestration.tools.delegation import (
    agent_delegation_tool,
    workflow_delegation_tool,
)
from financeclaw.orchestration.tools.memory import default_memory_tools


@dataclass(frozen=True, slots=True)
class FinanceClawComponents:
    """组合根的装配结果容器，持有平台运行所需的全部组件实例。

    使用场景：由 ``build_components`` 构造并返回；应用入口、API 层与测试
    夹具从中取用目录、工厂与各仓储，未启用持久化时相应字段为 None。

    Attributes:
        settings: 全局配置，涵盖环境、模型、数据库、存储与观测等。
        tool_catalog: 治理后的工具目录，含本地工具、MCP 报价工具、记忆与委派工具。
        tool_policy: 工具调用策略，承载调用校验与治理规则。
        audit: 审计仓储；未注入且未启用持久化时为内存实现。
        model_profiles: 模型档案目录，登记主模型与降级候选档案。
        agent_profiles: Agent 档案目录，登记顶层与领域 Agent 档案。
        model_factory: 模型工厂，依据模型档案构建模型实例。
        agent_factory: Agent 工厂，依据 Agent 档案与工具目录构建 ReAct Agent。
        database: 应用数据库连接；未启用持久化时为 None。
        conversation_repository: 会话仓储；未启用持久化时为 None。
        context_builder: 上下文预算构建器，控制注入模型的上下文规模。
        summary_service: 会话摘要服务，负责分段与层级摘要生成。
        artifact_service: 制品服务，负责制品登记与内容读写。
        memory_service: 长期记忆服务；无会话仓储时不可用，为 None。
        workflow_catalog: Workflow 目录，登记已发布的流程定义。
        workflow_repository: Workflow 仓储；未启用持久化时为 None。
        delegation_repository: Agent 委派记录仓储；未启用持久化时为 None。
        outbox_repository: Outbox 仓储，支撑事件最终一致外发；未启用持久化时为 None。
        ziwei_service: 紫微预检、计算与制品用例；候选功能未启用时为 None。

    """

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
    ziwei_service: ZiweiService | None = None

    @property
    def default_agent_profile(self) -> AgentProfile:
        """返回目录中当前可用的最高版本 finance_agent 档案。

        候选功能开关决定装配哪些版本；已创建会话使用它保存的固定版本，
        不应在每轮调用时重新选择默认档案。

        Returns:
            顶层财务 Agent 的 ``AgentProfile``。

        """
        return self.agent_profiles.resolve("finance_agent")


def build_components(
    settings: FinanceClawSettings | None = None,
    *,
    tool_catalog: ToolCatalog | None = None,
    audit: AuditRepository | None = None,
    enable_persistence: bool = False,
) -> FinanceClawComponents:
    """依据配置装配 FinanceClaw 全部组件，返回可直接运行的组件集合。

    Args:
        settings: 全局配置；缺省时构造默认配置（从环境变量读取）。
        tool_catalog: 外部注入的工具目录；缺省时按配置构建默认目录。
        audit: 外部注入的审计仓储；缺省时按持久化开关选择具体实现。
        enable_persistence: 是否启用数据库持久化（会话、制品、Workflow、委派、Outbox）。

    Returns:
        装配完成的 ``FinanceClawComponents``。

    """
    # 1. 加载配置：未显式传入时使用默认构造（从环境变量读取）。
    settings = settings or FinanceClawSettings()

    database: ApplicationDatabase | None = None
    conversation_repository: ConversationRepository | None = None
    context_builder: ConversationContextBuilder | None = None
    summary_service: SummaryService | None = None
    artifact_service: ArtifactService | None = None
    workflow_repository: WorkflowRepository | None = None
    delegation_repository: DelegationRepository | None = None
    outbox_repository: OutboxRepository | None = None
    # 2. 按需装配持久化设施：数据库、会话/制品/Workflow/委派/Outbox 仓储及派生服务。
    if enable_persistence:
        # 2.1 建立数据库连接，并可选自动初始化表结构（便于开发与首次部署）。
        database = ApplicationDatabase(
            settings.database_url.get_secret_value(),
            statement_timeout_seconds=settings.database_statement_timeout_seconds,
        )
        if settings.database_auto_create_schema:
            database.initialize_schema()
        # 2.2 装配会话仓储、上下文预算构建器与摘要服务。
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
        # 2.3 依据配置选择制品后端（S3 或本地文件系统），并装配制品服务。
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
        # 2.4 装配 Workflow、委派与 Outbox 仓储。
        workflow_repository = SqlAlchemyWorkflowRepository(database.session_factory)
        delegation_repository = SqlAlchemyDelegationRepository(database.session_factory)
        outbox_repository = SqlAlchemyOutboxRepository(database.session_factory)

    # 3. 选择审计实现：外部注入优先；否则有数据库用 SQL 实现，兜底内存实现。
    if audit is not None:
        effective_audit = audit
    elif database is not None:
        effective_audit = SqlAlchemyAuditRepository(database.session_factory)
    else:
        effective_audit = InMemoryAuditRepository()

    # 4. 出站网络策略校验：逐一校验模型提供方、Agent Server 与各类外部端点。
    # 4.1 校验模型提供方地址（离线模式或未配置时跳过）。
    if not settings.offline_model and settings.provider_base_url:
        EgressPolicy(
            settings.egress_allowed_hosts,
            require_https=settings.environment.value in {"staging", "production"},
        ).validate(settings.provider_base_url)
    # 4.2 校验内部 Agent Server 地址（允许内网主机与 HTTP）。
    EgressPolicy(
        settings.internal_service_hosts,
        require_https=False,
        allow_private_hosts=True,
    ).validate(settings.agent_server_url)
    # 4.3 生产环境额外校验认证、LangSmith 与 OpenTelemetry 观测端点。
    if settings.environment.value == "production" and settings.oidc_jwks_url:
        EgressPolicy(settings.egress_allowed_hosts).validate(settings.oidc_jwks_url)
        EgressPolicy(settings.egress_allowed_hosts).validate(settings.langsmith_endpoint)
        if settings.otel_exporter_endpoint:
            EgressPolicy(settings.egress_allowed_hosts).validate(settings.otel_exporter_endpoint)
        if settings.otel_metrics_exporter_endpoint:
            EgressPolicy(settings.egress_allowed_hosts).validate(
                settings.otel_metrics_exporter_endpoint
            )
    # 4.4 自定义 S3 端点按内部服务策略校验（允许内网与 HTTP）。
    if settings.artifact_s3_endpoint_url:
        EgressPolicy(
            settings.internal_service_hosts,
            require_https=False,
            allow_private_hosts=True,
        ).validate(settings.artifact_s3_endpoint_url)

    # 5. 装配长期记忆服务：依赖会话仓储，未启用持久化时跳过。
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
    # 6. 构建基础工具目录：本地工具 + MCP 报价工具 + 记忆工具；外部注入优先。
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
    # 工具调用策略与目录解耦，使用默认规则集独立实例化。
    tool_policy = ToolPolicy()
    # 7. 装配 Workflow 目录：仅在制品服务可用（已启用持久化）时注册组合复盘流程。
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
                execution=getattr(conversation_repository, "execution", None),
            ),
        )
        if artifact_service is not None
        else ()
    )

    # 8. 装配模型档案目录：主模型 + 按序降级的候选模型链（fallback）。
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
    from financeclaw.orchestration.agents.release import configuration_fingerprint

    model_release = (primary_profile, *fallback_profiles)
    # 9. 构建模型工厂：依据档案目录实例化模型客户端。
    model_factory = ModelFactory(
        model_profiles,
        api_key=settings.provider_api_key,
        base_url=settings.provider_base_url,
    )
    # 10. 定义只读市场调研领域 Agent：仅暴露市场类工具，不允许二次委派或写操作。
    domain_tool_refs = tuple(
        ToolRef(
            tool_id=managed.governance.tool_id,
            version=managed.governance.version,
        )
        for tool_id in ("market_snapshot", "get_demo_quote")
        if any(key[0] == tool_id for key in base_tool_catalog)
        for managed in (base_tool_catalog.resolve(tool_id),)
    )
    from financeclaw.modules.delegation.market_research import (
        MarketResearchInput,
        MarketResearchResult,
    )
    from financeclaw.modules.interactions import InteractionPoint

    domain_agent_profile = AgentProfile(
        agent_id="market_research_agent",
        version="1.2.0",
        assistant_id="market_research_agent_v1_2_0",
        context_policy="delegated-task-only-v1",
        input_schema=MarketResearchInput,
        output_schema=MarketResearchResult,
        interaction_points=(
            InteractionPoint(
                point_id="research_scope",
                kind="input",
                question="请补充本次研究的时间区间。",
                response_schema={
                    "type": "object",
                    "properties": {
                        "analysis_period": {"type": "string", "minLength": 1, "maxLength": 128}
                    },
                    "required": ["analysis_period"],
                    "additionalProperties": False,
                },
            ),
            InteractionPoint(
                point_id="research_focus",
                kind="choice",
                question="希望重点研究哪个方面？",
                options=("价格与走势", "风险与限制", "综合概览"),
            ),
        ),
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
            " Return the declared structured outcome. If required facts or the subject "
            "are unclear, return needs_clarification with a precise question and "
            "missing_fields before doing substantive work. After saving useful progress, "
            "use only the declared request_user tools for necessary input or choices, "
            "then continue the same task using the user's actual answer. "
            "Return partial or unsupported with limitations when success cannot be established."
        ),
        allowed_tools=domain_tool_refs,
        configuration_fingerprint=configuration_fingerprint(
            model_release,
            settings.offline_model,
            MarketResearchInput.model_json_schema(),
            MarketResearchResult.model_json_schema(),
            [
                base_tool_catalog.resolve(ref.tool_id, ref.version).governance
                for ref in domain_tool_refs
            ],
        ),
        memory_policy="none",
        max_model_calls=6,
        max_tool_calls=8,
    )
    # 为每个已发布且激活的 Workflow 与领域 Agent 生成委派工具，供顶层 Agent 调用。
    delegation_tools = (
        *(
            workflow_delegation_tool(definition)
            for definition in workflow_catalog.published()
            if definition.status is WorkflowStatus.ACTIVE
        ),
        agent_delegation_tool(domain_agent_profile),
    )
    # 把委派工具并入目录，使顶层 Agent 能以工具调用形式触发 Workflow/Agent 委派。
    tool_catalog = ToolCatalog((*base_tool_catalog.values(), *delegation_tools))
    # 11. 定义顶层 finance_agent 档案：ReAct 决策直接回答、Tool、Workflow 或委派。
    agent_profile = AgentProfile(
        agent_id="finance_agent",
        version="1.2.0",
        assistant_id="finance_agent_v1_2_0",
        configuration_fingerprint=configuration_fingerprint(
            model_release,
            settings.offline_model,
            domain_agent_profile.model_dump(mode="python"),
            [managed.governance for managed in tool_catalog.latest()],
        ),
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
            " Inspect delegated outcome, not just transport status: needs_clarification means ask "
            "the user and end this turn; after their answer start a new bounded delegation. "
            "Never report failed, unsupported or partial child results as success. Preserve the "
            "subject, source, evidence and limitations; do not switch tools to bypass a rejection."
        ),
        allowed_tools=tuple(
            ToolRef(tool_id=managed.governance.tool_id, version=managed.governance.version)
            for managed in tool_catalog.latest()
        ),
        memory_policy="stage3-governed-v1",
    )
    # Stage 7 工具不进入已发布根 1.2.0 的白名单；仅显式启用时发布新的根 1.3.0。
    from financeclaw.kernel import DataClassification
    from financeclaw.modules.ziwei.models import ZiweiConvention
    from financeclaw.modules.ziwei.service import ZiweiCalculationService
    from financeclaw.orchestration.agents.ziwei import ziwei_profile
    from financeclaw.orchestration.tools.ziwei import ziwei_tools

    ziwei_service = None
    convention = ZiweiConvention()
    if settings.ziwei_enabled:
        from financeclaw.infrastructure.ziwei.x_iztro import XIztroEngine

        ziwei_service = ZiweiService(
            ZiweiCalculationService(XIztroEngine(), convention),
            hmac_key=settings.ziwei_hmac_key.get_secret_value().encode(),
            key_version=settings.ziwei_key_version,
            artifacts=artifact_service,
            projection_bytes=settings.ziwei_projection_bytes,
        )
    chart_tools = ziwei_tools(ziwei_service)
    specialist = ziwei_profile(
        configuration_fingerprint(
            model_release,
            settings.offline_model,
            convention,
            settings.ziwei_enabled,
            settings.ziwei_key_version,
            settings.ziwei_projection_bytes,
            settings.context_input_limit,
            settings.context_reserved_output,
            settings.artifact_inline_bytes,
            [managed.governance for managed in chart_tools],
        )
    )
    ziwei_delegate = agent_delegation_tool(specialist)
    ziwei_delegate.tool.metadata = {"preserve_result": True}
    profiles = [agent_profile, domain_agent_profile, specialist]
    if settings.ziwei_enabled:
        stage7_root = agent_profile.model_copy(
            update={
                "version": "1.3.0",
                "assistant_id": "finance_agent_v1_3_0",
                "deployment_revision": "stage7-candidate/1",
                "data_classification": DataClassification.CONFIDENTIAL,
                "allowed_tools": (
                    *agent_profile.allowed_tools,
                    ToolRef(
                        tool_id=ziwei_delegate.tool.name,
                        version=specialist.version,
                    ),
                ),
                "configuration_fingerprint": configuration_fingerprint(
                    agent_profile.configuration_fingerprint,
                    specialist.model_dump(mode="json"),
                    ziwei_delegate.governance,
                    "protected-structured-result-v1",
                ),
                "system_prompt_template": agent_profile.system_prompt_template
                + (
                    " Handle bounded Ziwei (紫微斗数) traditional-culture requests by delegating "
                    "to ziwei_doushu_agent. Never calculate star positions yourself. Collect birth "
                    "calendar/date, local clock or shichen, time basis, place and sex_for_chart; "
                    "do not invent missing fields. Set requested level and explicit target. "
                    "Preserve chart evidence and warnings. Astrology is not financial evidence. "
                    "Never store birth data or divination conclusions in long-term memory."
                ),
            }
        )
        profiles.append(stage7_root)
    agent_profiles = AgentProfileCatalog(profiles)
    tool_catalog = ToolCatalog((*tool_catalog.values(), *chart_tools, ziwei_delegate))

    # 12. 构建 Agent 工厂：绑定模型、工具、策略、审计与各类服务。
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
    # 13. 汇总返回组件集合。
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
        ziwei_service=ziwei_service,
    )
