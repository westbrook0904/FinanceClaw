"""共享应用数据库与存储资源的装配；服务运行用例由各自包负责。"""

from dataclasses import dataclass

from financeclaw.shared.artifacts.repository import SqlAlchemyArtifactRepository
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.artifacts.storage import LocalArtifactStore, S3ArtifactStore
from financeclaw.shared.audit.repository import (
    AuditRepository,
    InMemoryAuditRepository,
    SqlAlchemyAuditRepository,
)
from financeclaw.shared.conversation.repository import (
    ConversationRepository,
    SqlAlchemyConversationRepository,
)
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.security.egress import EgressPolicy
from financeclaw.shared.infrastructure.settings import ArtifactBackend, FinanceClawSettings
from financeclaw.shared.outbox.repository import OutboxRepository, SqlAlchemyOutboxRepository


@dataclass(frozen=True, slots=True)
class ApplicationResources:
    """同进程复用一组 Session 和共享事实仓储；多个服务可连接同一数据库。"""

    settings: FinanceClawSettings
    audit: AuditRepository
    database: ApplicationDatabase | None = None
    conversation_repository: ConversationRepository | None = None
    artifact_service: ArtifactService | None = None
    outbox_repository: OutboxRepository | None = None


def build_resources(
    settings: FinanceClawSettings | None = None,
    *,
    audit: AuditRepository | None = None,
    enable_persistence: bool = False,
) -> ApplicationResources:
    """构建共享资源；不会编译图、创建模型或连接 Channel。"""
    # 1. 加载配置：未显式传入时使用默认构造（从环境变量读取）。
    settings = settings or FinanceClawSettings()

    database: ApplicationDatabase | None = None
    conversation_repository: ConversationRepository | None = None
    artifact_service: ArtifactService | None = None
    outbox_repository: OutboxRepository | None = None
    # 2. 按需装配持久化设施：数据库、会话/制品/Outbox 仓储及派生服务。
    if enable_persistence:
        # 2.1 建立数据库连接，并可选自动初始化表结构（便于开发与首次部署）。
        database = ApplicationDatabase(
            settings.database_url.get_secret_value(),
            statement_timeout_seconds=settings.database_statement_timeout_seconds,
        )
        if settings.database_auto_create_schema:
            database.initialize_schema()
        # 2.2 装配会话仓储与摘要服务。
        concrete_repository = SqlAlchemyConversationRepository(database.session_factory)
        conversation_repository = concrete_repository

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
            retention_days=settings.artifact_retention_days,
        )
        # 2.4 装配共享 Outbox 仓储。
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
    if settings.artifact_backend == "s3" and settings.artifact_s3_endpoint_url:
        EgressPolicy(
            settings.internal_service_hosts,
            require_https=False,
            allow_private_hosts=True,
        ).validate(settings.artifact_s3_endpoint_url)

    return ApplicationResources(
        settings,
        effective_audit,
        database,
        conversation_repository,
        artifact_service,
        outbox_repository,
    )
