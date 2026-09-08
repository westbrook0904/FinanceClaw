"""AgentServer 装配入口：只创建图、模型、工具及执行端依赖。"""

from dataclasses import dataclass

from financeclaw.agent_server.agents.factory import AgentFactory
from financeclaw.agent_server.context.builder import ContextBudget, ConversationContextBuilder
from financeclaw.agent_server.domains.ziwei.application import ZiweiService
from financeclaw.agent_server.graphs.workflows.portfolio_review_v1 import (
    portfolio_review_definition,
)
from financeclaw.agent_server.llm.factory import ModelFactory
from financeclaw.agent_server.memory.policy import MemoryPolicy
from financeclaw.agent_server.memory.service import LongTermMemoryService
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.delegation import (
    agent_delegation_tool,
    workflow_delegation_tool,
)
from financeclaw.agent_server.tools.local import default_local_tools
from financeclaw.agent_server.tools.mcp import managed_mcp_quote_tool
from financeclaw.agent_server.tools.memory import default_memory_tools
from financeclaw.agent_server.tools.policy import ToolPolicy
from financeclaw.kernel.agents import AgentProfile, AgentProfileCatalog
from financeclaw.kernel.models import ModelProfileCatalog
from financeclaw.kernel.tool_catalog import ToolRelease, ToolReleaseCatalog
from financeclaw.kernel.workflows.catalog import WorkflowCatalog
from financeclaw.kernel.workflows.models import WorkflowStatus
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.audit.repository import (
    AuditRepository,
)
from financeclaw.shared.conversation.repository import (
    ConversationRepository,
)
from financeclaw.shared.conversation.summaries import SummaryService
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.resources import ApplicationResources, build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.outbox.repository import OutboxRepository
from financeclaw.shared.releases.catalog import build_release_catalogs


@dataclass(frozen=True, slots=True)
class AgentServerComponents:
    """AgentServer 装配结果，持有图、模型、工具及共享事实访问组件。

    使用场景：由 ``build_components`` 构造并返回；AgentServer 入口与测试
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
    outbox_repository: OutboxRepository | None = None
    ziwei_service: ZiweiService | None = None

    @property
    def default_agent_profile(self) -> AgentProfile:
        """返回仍由旧 BFF 准入的 finance_agent@1.4.0 档案。

        HF-1 只注册新图，默认版本须等 HF-2 新驱动完成后显式切换。
        候选功能开关决定当前发布的工具配置；已创建会话使用它保存的固定版本，
        不应在每轮调用时重新选择默认档案。

        Returns:
            顶层财务 Agent 的 ``AgentProfile``。

        """
        return self.agent_profiles.resolve("finance_agent", "1.4.0")


def build_components(
    settings: FinanceClawSettings | None = None,
    *,
    tool_catalog: ToolCatalog | None = None,
    audit: AuditRepository | None = None,
    enable_persistence: bool = False,
    resources: ApplicationResources | None = None,
    enable_subgraphs: bool = False,
    resource_concurrency: int = 8,
) -> AgentServerComponents:
    """装配执行端；不创建 Coordinator 客户端、业务服务或飞书连接。"""
    resources = resources or build_resources(
        settings, audit=audit, enable_persistence=enable_persistence
    )
    settings = resources.settings
    database = resources.database
    conversation_repository = resources.conversation_repository
    summary_service = resources.summary_service
    artifact_service = resources.artifact_service
    outbox_repository = resources.outbox_repository
    effective_audit = resources.audit
    context_builder = None
    if conversation_repository is not None:
        context_builder = ConversationContextBuilder(
            conversation_repository,
            ContextBudget(
                model_input_limit=settings.context_input_limit,
                reserved_output_tokens=settings.context_reserved_output,
                system_policy_reserve=settings.context_system_policy_reserve,
                tool_schema_reserve=settings.context_tool_schema_reserve,
                safety_margin=settings.context_safety_margin,
            ),
        )
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

    releases = build_release_catalogs(
        settings,
        enable_persistence=artifact_service is not None,
        include_subgraphs=enable_subgraphs,
        base_tool_catalog=ToolReleaseCatalog(
            ToolRelease(item.governance) for item in base_tool_catalog.values()
        ),
    )
    model_profiles = releases.model_profiles
    agent_profiles = releases.agent_profiles
    model_factory = ModelFactory(
        model_profiles, api_key=settings.provider_api_key, base_url=settings.provider_base_url
    )
    from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine
    from financeclaw.agent_server.domains.ziwei.service import ZiweiCalculationService
    from financeclaw.agent_server.tools.ziwei import ziwei_tools
    from financeclaw.kernel.ziwei import ZiweiConvention

    ziwei_service = None
    if settings.ziwei_enabled:
        ziwei_service = ZiweiService(
            ZiweiCalculationService(XIztroEngine(), ZiweiConvention()),
            hmac_key=settings.ziwei_hmac_key.get_secret_value().encode(),
            key_version=settings.ziwei_key_version,
            artifacts=artifact_service,
            projection_bytes=settings.ziwei_projection_bytes,
        )
    chart_tools = ziwei_tools(ziwei_service)
    ziwei_delegate = agent_delegation_tool(agent_profiles.resolve("ziwei_doushu_agent", "2.0.0"))
    ziwei_delegate.tool.metadata = {"preserve_result": True}
    tool_catalog = ToolCatalog(
        (
            *base_tool_catalog.values(),
            *(
                workflow_delegation_tool(definition)
                for definition in workflow_catalog.published()
                if definition.status is WorkflowStatus.ACTIVE
            ),
            agent_delegation_tool(agent_profiles.resolve("market_research_agent", "1.2.0")),
            *chart_tools,
            ziwei_delegate,
        )
    )
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
        resource_concurrency=resource_concurrency,
    )
    if enable_subgraphs:
        from financeclaw.agent_server.tools.subgraph_assembly import assemble_subgraph_tools

        composites = assemble_subgraph_tools(
            releases,
            agent_factory,
            settings=settings,
            ziwei_service=ziwei_service,
        )
        tool_catalog = ToolCatalog((*tool_catalog.values(), *composites))
        agent_factory.tool_catalog = tool_catalog
    # 13. 汇总返回组件集合。
    return AgentServerComponents(
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
        outbox_repository=outbox_repository,
        ziwei_service=ziwei_service,
    )
