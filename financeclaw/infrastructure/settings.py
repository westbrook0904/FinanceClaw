"""集中声明环境变量配置，并校验生产环境安全约束。"""

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """应用部署环境；生产环境会触发更严格的配置校验。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        DEVELOPMENT: 本地开发环境，允许使用便利性配置。
        TEST: 自动化测试环境，依赖应可替换且结果可复现。
        STAGING: 生产前验证环境，安全约束应接近生产。
        PRODUCTION: 生产环境，强制启用完整鉴权与网络安全校验。
    """

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ArtifactBackend(StrEnum):
    """制品二进制内容使用的存储后端。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        LOCAL: 制品内容写入受控的本地文件系统。
        S3: 制品内容写入 S3 兼容对象存储。
    """

    LOCAL = "local"
    S3 = "s3"


class FinanceClawSettings(BaseSettings):
    """汇总环境变量配置，并阻止不安全的生产环境组合启动。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        environment: 当前部署环境，决定默认值和安全校验强度。
        model: 供应商模型名称。
        fallback_models: 主模型不可用时按顺序尝试的供应商模型名称。
        provider_base_url: 模型供应商兼容 API 的基础 URL；为空时使用 SDK 默认地址。
        provider_api_key: 模型供应商 API 凭证，使用 SecretStr 避免意外日志泄露。
        offline_model: 是否使用确定性离线模型替代外部供应商。
        debug_full_io: 是否记录脱敏后的完整模型与工具输入输出；生产环境默认关闭。
        log_level: 应用根日志级别。
        model_timeout_seconds: 该操作允许的最长时间（秒）。
        model_max_tokens: 该步骤可用或实际使用的 token 数量。
        model_max_retries: 主模型及回退模型调用允许的最大重试次数。
        read_max_attempts: 只读工具发生瞬时错误时允许的最大尝试次数。
        approval_timeout_seconds: 该操作允许的最长时间（秒）。
        workflow_run_timeout_seconds: 该操作允许的最长时间（秒）。
        mcp_timeout_seconds: 该操作允许的最长时间（秒）。
        agent_server_url: LangGraph Agent Server 的基础 URL。
        agent_server_timeout_seconds: 该操作允许的最长时间（秒）。
        agent_server_service_token: 调用 Agent Server 时携带的可选服务凭证。
        oidc_issuer: JWT 必须匹配的 OIDC 签发者。
        oidc_audience: JWT 必须包含的目标受众。
        oidc_jwks_url: 获取 OIDC 公钥集合的 HTTPS 地址。
        oidc_algorithms: 验证 JWT 签名时允许的算法白名单。
        oidc_tenant_claim: JWT 中承载租户标识的 claim 名称。
        oidc_subject_claim: JWT 中承载主体标识的 claim 名称。
        oidc_scope_claim: JWT 中承载权限域的 claim 名称。
        oidc_clock_skew_seconds: 该操作允许的最长时间（秒）。
        oidc_jwks_timeout_seconds: 该操作允许的最长时间（秒）。
        bff_auth_token: 非生产 BFF 模式使用的静态 Bearer 凭证。
        bff_tenant_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        bff_subject_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        bff_scopes: BFF 静态身份拥有的权限域。
        langsmith_project: LangSmith 追踪写入的项目名称。
        langsmith_endpoint: LangSmith API 端点。
        langsmith_trace_sample_rate: LangSmith 链路追踪采样比例。
        langsmith_hide_inputs: 是否禁止向 LangSmith 发送原始输入。
        langsmith_hide_outputs: 是否禁止向 LangSmith 发送原始输出。
        otel_exporter_endpoint: OpenTelemetry 追踪 OTLP HTTP 导出端点。
        otel_metrics_exporter_endpoint: OpenTelemetry 指标 OTLP HTTP 导出端点。
        otel_trace_sample_rate: OpenTelemetry 追踪采样比例。
        otel_service_name: 遥测数据中标识本服务的资源名称。
        database_url: 数据库连接 URL，使用 SecretStr 防止凭证进入日志。
        database_auto_create_schema: 启动时是否创建缺失表；只适合开发和测试。
        database_statement_timeout_seconds: 该操作允许的最长时间（秒）。
        artifact_backend: 制品内容使用本地文件还是 S3 兼容存储。
        artifact_root: 本地制品存储的受控根目录。
        artifact_inline_bytes: 工具结果可直接内联返回的最大字节数。
        artifact_s3_bucket: S3 制品存储桶；选择 S3 后端时必填。
        artifact_s3_prefix: 所有制品对象键使用的公共前缀。
        artifact_s3_endpoint_url: 自建 S3 兼容服务的可选端点。
        artifact_s3_region: S3 客户端使用的区域。
        artifact_s3_sse_algorithm: 上传制品时请求的服务端加密算法。
        artifact_s3_kms_key_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        artifact_s3_timeout_seconds: 该操作允许的最长时间（秒）。
        artifact_s3_max_pool_connections: S3 HTTP 连接池允许的最大并发连接数。
        egress_allowed_hosts: 普通外部请求允许访问的主机白名单。
        internal_service_hosts: 平台内部服务主机白名单，可按策略允许私网地址。
        readiness_timeout_seconds: 该操作允许的最长时间（秒）。
        shutdown_timeout_seconds: 该操作允许的最长时间（秒）。
        outbox_batch_size: 发布者单次领取的最大事件数量。
        outbox_max_attempts: 事件进入死信状态前允许的最大发布尝试次数。
        api_p95_target_ms: HTTP 请求 P95 延迟目标，单位毫秒。
        context_input_limit: 模型上下文允许使用的最大输入 token 数。
        context_reserved_output: 为模型输出预留、不得被输入占用的 token 数。
        context_system_policy_reserve: 为系统策略内容预留的 token 数。
        context_tool_schema_reserve: 为工具 schema 预留的 token 数。
        context_safety_margin: 为分词估算误差预留的 token 安全余量。
        summary_segment_messages: 触发最低层摘要的消息分段大小。
        summary_hierarchy_segments: 合并为高层摘要的相邻摘要数量。
        memory_recall_tokens: 该步骤可用或实际使用的 token 数量。
        memory_recall_limit: 单次模型调用最多注入的长期记忆条数。
        memory_auto_commit_low_risk_preferences: 是否自动提交有证据支持的低风险偏好记忆。
    """

    model_config = SettingsConfigDict(
        env_prefix="FINANCECLAW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    model: str = "openai:deepseek-v4-pro"
    fallback_models: tuple[str, ...] = ()
    provider_base_url: str | None = "https://api.deepseek.com"
    provider_api_key: SecretStr | None = None
    offline_model: bool = False
    debug_full_io: bool = True
    log_level: str = "INFO"
    model_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    model_max_tokens: int = Field(default=4096, ge=64, le=384_000)
    model_max_retries: int = Field(default=2, ge=0, le=8)
    read_max_attempts: int = Field(default=3, ge=1, le=8)
    approval_timeout_seconds: int = Field(default=900, ge=30, le=86_400)
    workflow_run_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    mcp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    agent_server_url: str = "http://127.0.0.1:2024"
    agent_server_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    agent_server_service_token: SecretStr | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256", "ES256")
    oidc_tenant_claim: str = "tenant_id"
    oidc_subject_claim: str = "sub"
    oidc_scope_claim: str = "scope"
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    oidc_jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    bff_auth_token: SecretStr | None = None
    bff_tenant_id: str = "development"
    bff_subject_id: str = "developer"
    bff_scopes: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {
                "market:read",
                "tools:read",
                "watchlist:write",
                "artifacts:read",
                "memory:read",
                "memory:write",
                "memory:delete",
                "portfolio:review",
                "workflows:approve",
            }
        )
    )
    langsmith_project: str = "financeclaw-stage5-development"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_trace_sample_rate: float = Field(default=1.0, ge=0, le=1)
    langsmith_hide_inputs: bool = False
    langsmith_hide_outputs: bool = False
    otel_exporter_endpoint: str | None = None
    otel_metrics_exporter_endpoint: str | None = None
    otel_trace_sample_rate: float = Field(default=1.0, ge=0, le=1)
    otel_service_name: str = "financeclaw-api"
    database_url: SecretStr = SecretStr("sqlite+pysqlite:///./.financeclaw/financeclaw.db")
    database_auto_create_schema: bool = True
    database_statement_timeout_seconds: int = Field(default=30, ge=1, le=300)
    artifact_backend: ArtifactBackend = ArtifactBackend.LOCAL
    artifact_root: str = ".financeclaw/artifacts"
    artifact_inline_bytes: int = Field(default=16_384, ge=256, le=10_000_000)
    artifact_s3_bucket: str | None = None
    artifact_s3_prefix: str = "financeclaw"
    artifact_s3_endpoint_url: str | None = None
    artifact_s3_region: str | None = None
    artifact_s3_sse_algorithm: str = "AES256"
    artifact_s3_kms_key_id: str | None = None
    artifact_s3_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    artifact_s3_max_pool_connections: int = Field(default=50, ge=1, le=500)
    egress_allowed_hosts: frozenset[str] = Field(
        default_factory=lambda: frozenset({"api.deepseek.com"})
    )
    internal_service_hosts: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {"127.0.0.1", "localhost", "agent-server", "artifact-store"}
        )
    )
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    shutdown_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    outbox_batch_size: int = Field(default=100, ge=1, le=1_000)
    outbox_max_attempts: int = Field(default=8, ge=1, le=100)
    api_p95_target_ms: int = Field(default=2_500, ge=1)
    context_input_limit: int = Field(default=32_768, ge=1_024)
    context_reserved_output: int = Field(default=4_096, ge=64)
    context_system_policy_reserve: int = Field(default=2_048, ge=0)
    context_tool_schema_reserve: int = Field(default=4_096, ge=0)
    context_safety_margin: int = Field(default=1_024, ge=0)
    summary_segment_messages: int = Field(default=12, ge=2, le=1_000)
    summary_hierarchy_segments: int = Field(default=8, ge=2, le=1_000)
    memory_recall_tokens: int = Field(default=768, ge=64, le=8_192)
    memory_recall_limit: int = Field(default=5, ge=1, le=20)
    memory_auto_commit_low_risk_preferences: bool = False

    @model_validator(mode="after")
    def protect_production(self) -> "FinanceClawSettings":
        """校验生产环境必须启用的鉴权、HTTPS 和安全配置。"""
        if self.environment is Environment.PRODUCTION and self.debug_full_io:
            raise ValueError("debug_full_io must be disabled in production")
        if self.environment is Environment.PRODUCTION and self.offline_model:
            raise ValueError("offline_model is only valid for development and tests")
        if self.environment is Environment.PRODUCTION:
            if self.bff_auth_token is not None:
                raise ValueError(
                    "bff_auth_token is a development adapter and is forbidden in production"
                )
            oidc_values = (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
            if not all(oidc_values):
                raise ValueError("oidc_issuer, oidc_audience and oidc_jwks_url are required")
            if any(
                algorithm.startswith("HS") or algorithm == "none"
                for algorithm in self.oidc_algorithms
            ):
                raise ValueError("production OIDC must use configured asymmetric algorithms")
            if not self.oidc_algorithms:
                raise ValueError("at least one OIDC algorithm is required")
            if urlparse(self.oidc_issuer or "").scheme != "https":
                raise ValueError("production oidc_issuer must use HTTPS")
            if urlparse(self.oidc_jwks_url or "").scheme != "https":
                raise ValueError("production oidc_jwks_url must use HTTPS")
            if self.agent_server_service_token is None:
                raise ValueError("agent_server_service_token is required in production")
            database_url = self.database_url.get_secret_value()
            if not database_url.startswith(("postgresql+psycopg://", "postgresql://")):
                raise ValueError("production database_url must use PostgreSQL")
            if self.database_auto_create_schema:
                raise ValueError("database_auto_create_schema must be disabled in production")
            if self.artifact_backend is not ArtifactBackend.S3 or not self.artifact_s3_bucket:
                raise ValueError("production artifact storage must use a configured S3 bucket")
            if not self.otel_exporter_endpoint or not self.otel_metrics_exporter_endpoint:
                raise ValueError(
                    "OTel trace and metric exporter endpoints are required in production"
                )
            if self.langsmith_trace_sample_rate > 0.1:
                raise ValueError("production LangSmith trace sampling must not exceed 0.1")
            if not self.langsmith_hide_inputs or not self.langsmith_hide_outputs:
                raise ValueError("production LangSmith traces must hide inputs and outputs")

        allowed_algorithms = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not set(self.oidc_algorithms).issubset(allowed_algorithms):
            raise ValueError("oidc_algorithms contains an unsupported or symmetric algorithm")
        if self.artifact_s3_sse_algorithm not in {"AES256", "aws:kms", "aws:kms:dsse"}:
            raise ValueError("unsupported S3 server-side encryption algorithm")
        if self.artifact_s3_kms_key_id and not self.artifact_s3_sse_algorithm.startswith("aws:kms"):
            raise ValueError("artifact_s3_kms_key_id requires aws:kms encryption")
        return self
