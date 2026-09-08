"""Coordination 装配入口：发布声明、执行后端和跨运行生命周期服务。"""

from dataclasses import dataclass

from financeclaw.coordination.api import (
    ConversationRunService,
    CoordinatorAdmission,
    DelegationService,
    RunService,
    TargetResolver,
    WorkflowService,
)
from financeclaw.coordination.backends.langgraph import LangGraphAgentServerClient
from financeclaw.coordination.backends.ports.agent_server import AgentServerClient
from financeclaw.coordination.delegation.repository import SqlAlchemyDelegationRepository
from financeclaw.coordination.workflows.repository import SqlAlchemyWorkflowRepository
from financeclaw.shared.infrastructure.resources import ApplicationResources, build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.catalog import ReleaseCatalogs, build_release_catalogs


@dataclass(frozen=True, slots=True)
class CoordinationServices:
    """可嵌入 BFF 的协调服务集合；Stage-8 将在此基础上装配独立进程。"""

    resources: ApplicationResources
    releases: ReleaseCatalogs
    client: AgentServerClient
    runs: RunService
    workflows: WorkflowService
    delegations: DelegationService
    conversations: ConversationRunService | CoordinatorAdmission
    background_repository: object | None = None
    background_releases: object | None = None


def build_coordination(
    settings: FinanceClawSettings | None = None,
    *,
    resources: ApplicationResources | None = None,
    client: AgentServerClient | None = None,
) -> CoordinationServices:
    """共享一个应用数据库，按 Port 注入后端；不构建任何执行图或模型。"""
    resources = resources or build_resources(settings, enable_persistence=True)
    settings = resources.settings
    if resources.database is None or resources.conversation_repository is None:
        raise RuntimeError("coordination persistence was not configured")
    releases = build_release_catalogs(settings, enable_persistence=True)
    client = client or LangGraphAgentServerClient(
        url=settings.agent_server_url,
        service_token=settings.agent_server_service_token.get_secret_value()
        if settings.agent_server_service_token is not None
        else None,
        timeout_seconds=settings.agent_server_timeout_seconds,
    )
    resolver = TargetResolver(
        tool_catalog=releases.tool_catalog,
        agent_profiles=releases.agent_profiles,
        workflow_catalog=releases.workflow_catalog,
    )
    workflow_service = WorkflowService(
        client,
        SqlAlchemyWorkflowRepository(resources.database.session_factory),
        releases.workflow_catalog,
        resources.audit,
    )
    delegation_service = DelegationService(
        client,
        SqlAlchemyDelegationRepository(resources.database.session_factory),
        workflow_service,
        releases.agent_profiles,
        resources.audit,
        conversation_repository=resources.conversation_repository,
        artifact_service=resources.artifact_service,
    )
    from financeclaw.coordination.application.releases import CoordinationReleases
    from financeclaw.coordination.repository import CoordinatorRepository

    # 生产查询始终经过持久投影；开关仅控制新受理，不重新激活查询驱动。
    background_repository = CoordinatorRepository(
        resources.database.session_factory,
        backend_instance_id=settings.coordinator_backend_instance_id,
        journal=resources.conversation_repository,
    )
    background_repository.require_schema()
    background_releases = CoordinationReleases(
        releases.agent_profiles, releases.workflow_catalog, delegation_service
    )
    conversations = CoordinatorAdmission(background_repository, background_releases, settings)
    return CoordinationServices(
        resources,
        releases,
        client,
        RunService(client, resolver),
        workflow_service,
        delegation_service,
        conversations,
        background_repository,
        background_releases,
    )


def build_coordinator(settings: FinanceClawSettings | None = None):
    """独立 Worker 的装配入口；不导入 BFF，不启动渠道或模型。"""
    from financeclaw.coordination.application.coordinator import Coordinator
    from financeclaw.coordination.backends.langgraph_backend import LangGraphBackend

    settings = settings or FinanceClawSettings()
    if not settings.coordinator_callback_url or settings.coordinator_webhook_token is None:
        raise RuntimeError(
            "Coordinator Worker requires a configured callback even when new admission is disabled"
        )
    services = build_coordination(settings)
    backend = LangGraphBackend(
        settings, services.background_repository, services.background_releases
    )
    coordinator = Coordinator(
        services.background_repository,
        services.background_releases,
        backend,
        settings,
        artifacts=services.resources.artifact_service,
    )
    return services, coordinator
