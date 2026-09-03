"""环境配置模块：基于 pydantic-settings 集中管理 FinanceClaw 的全部运行配置。

配置项从 ``FINANCECLAW_`` 前缀的环境变量与 ``.env`` 文件读取；生产环境的敏感
字段（API 密钥、数据库连接串等）由 Secret Manager 注入，并通过模型校验器
强制执行生产安全基线。
"""

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """部署环境枚举：标识当前运行所处的基础设施环境。

    使用场景：作为配置项参与组装根与环境差异分支（如生产强制 HTTPS、
    关闭调试输出、启用 OIDC 等），也用于遥测资源的环境标注。
    """

    # 开发环境：允许调试输出、离线模型与本地存储。
    DEVELOPMENT = "development"
    # 测试环境：供自动化测试使用，与开发环境同等宽松。
    TEST = "test"
    # 预发布环境：配置已贴近生产（如强制 HTTPS），但数据与流量隔离。
    STAGING = "staging"
    # 生产环境：执行最严格的安全基线校验。
    PRODUCTION = "production"


class ArtifactBackend(StrEnum):
    """产物存储后端枚举：决定运行产物（报告、文件）的持久化方式。

    使用场景：配置产物仓库时选择本地目录或 S3 兼容对象存储，
    生产环境强制使用 S3。
    """

    # 本地文件系统存储，用于开发与测试。
    LOCAL = "local"
    # S3 兼容对象存储，用于预发布与生产。
    S3 = "s3"


class FinanceClawSettings(BaseSettings):
    """FinanceClaw 全量运行配置：环境、模型、认证、观测、存储与预算等设置项。

    使用场景：bootstrap.py 组合根启动时构造唯一实例并向下传递；字段值从
    ``FINANCECLAW_`` 前缀的环境变量与 ``.env`` 文件读取，生产环境的敏感字段
    由 Secret Manager 注入。生产配置由 ``protect_production`` 校验安全基线，
    任何违规都会使配置加载失败（fail fast）。

    Attributes:
        environment: 部署环境，驱动各处的环境差异分支与安全基线强度。
        model: 主模型标识，格式为 ``provider:model``（如 ``openai:deepseek-v4-pro``）。
        fallback_models: 主模型不可用时的降级模型序列，按顺序尝试，格式同 ``model``。
        provider_base_url: LLM Provider 的 OpenAI 兼容 API 基址，启动时经出站 allowlist 校验。
        provider_api_key: Provider API 密钥，SecretStr 防止其在日志与报错中泄露。
        offline_model: 是否以离线桩模型运行，仅用于开发与测试，生产禁止开启。
        debug_full_io: 是否在日志与追踪中记录完整模型输入输出，生产必须关闭。
        log_level: 结构化日志级别（如 INFO、DEBUG）。
        model_timeout_seconds: 单次模型调用超时（秒），取值范围 (0, 600]。
        model_max_tokens: 单次生成允许的最大 token 数，取值范围 [64, 384000]。
        model_max_retries: 模型调用最大重试次数，取值范围 [0, 8]。
        read_max_attempts: 读操作（幂等查询类）的最大尝试次数，取值范围 [1, 8]。
        approval_timeout_seconds: 工作流人工审批的等待超时（秒），超时按未决处理。
        workflow_run_timeout_seconds: 工作流单次运行的软超时（秒）。
        mcp_timeout_seconds: 单次 MCP 工具调用的超时（秒）。
        agent_server_url: 内部 LangGraph Agent Server 地址，启动时按内部主机 allowlist 校验。
        agent_server_timeout_seconds: Agent Server 出站调用的超时（秒）。
        agent_server_service_token: Agent Server 服务间 Bearer 令牌，生产必填。
        oidc_issuer: OIDC 签发者标识，用于 JWT 校验，生产必填且必须为 HTTPS。
        oidc_audience: OIDC 受众（aud claim 的期望值），生产必填。
        oidc_jwks_url: JWKS 公钥集地址，生产必须为 HTTPS 并通过出站 allowlist 校验。
        oidc_algorithms: 允许的 JWT 签名算法，仅限非对称算法（RS*/ES* 系列）。
        oidc_tenant_claim: JWT 中承载租户 ID 的 claim 名。
        oidc_subject_claim: JWT 中承载主体 ID 的 claim 名。
        oidc_scope_claim: JWT 中承载作用域的 claim 名。
        oidc_clock_skew_seconds: JWT 时间断言校验容忍的时钟偏移（秒）。
        oidc_jwks_timeout_seconds: 拉取 JWKS 公钥集的超时（秒）。
        bff_auth_token: 开发期 BFF 使用的本地静态令牌，属于开发适配，生产禁止配置。
        bff_tenant_id: 开发期固定的租户 ID。
        bff_subject_id: 开发期固定的主体 ID。
        bff_scopes: 开发期授予 BFF 的作用域集合，模拟真实 OIDC scope。
        langsmith_project: LangSmith 追踪上报的项目名。
        langsmith_endpoint: LangSmith API 端点，启动时通过出站 allowlist 校验。
        langsmith_trace_sample_rate: LangSmith 追踪采样率 [0, 1]，生产不得超过 0.1。
        langsmith_hide_inputs: 是否在追踪中隐藏输入内容，生产必须开启。
        langsmith_hide_outputs: 是否在追踪中隐藏输出内容，生产必须开启。
        otel_exporter_endpoint: OpenTelemetry 链路（trace）的 OTLP HTTP 上报端点，生产必填。
        otel_metrics_exporter_endpoint: OpenTelemetry 指标的 OTLP HTTP 上报端点，生产必填。
        otel_trace_sample_rate: OpenTelemetry 链路采样率 [0, 1]。
        otel_service_name: 遥测资源中的服务名标识。
        database_url: 数据库连接串；默认本地 SQLite，生产必须为 PostgreSQL（Secret Manager 注入）。

        database_auto_create_schema: 启动时是否自动建表，生产必须关闭并改用 Alembic 迁移。
        database_statement_timeout_seconds: PostgreSQL 语句级超时（秒），防止慢查询拖垮连接池。
        artifact_backend: 产物存储后端（local/s3），生产必须使用 s3。
        artifact_root: 本地产物存储根目录。
        artifact_inline_bytes: 小于该字节数的产物可直接内联返回，避免额外的存储读取。
        artifact_s3_bucket: S3 产物桶名，生产必填。
        artifact_s3_prefix: S3 对象键前缀。
        artifact_s3_endpoint_url: 兼容 S3 协议的自定义端点（如 MinIO），为空则使用 AWS 默认端点。
        artifact_s3_region: S3 区域。
        artifact_s3_sse_algorithm: S3 服务端加密算法，仅允许 AES256/aws:kms/aws:kms:dsse。
        artifact_s3_kms_key_id: SSE-KMS 加密的 KMS 密钥 ID，必须配合 aws:kms 系算法使用。
        artifact_s3_timeout_seconds: 单次 S3 请求的超时（秒）。
        artifact_s3_max_pool_connections: S3 客户端连接池的最大连接数。
        egress_allowed_hosts: 外部出站主机 allowlist，默认仅放行模型 Provider 域名。
        internal_service_hosts: 内部服务主机 allowlist（Agent Server 等内网目标）。
        readiness_timeout_seconds: 就绪探测（含数据库 ping 等检查）的单项等待超时（秒）。
        shutdown_timeout_seconds: 优雅停机时等待在途请求与钩子完成的超时（秒）。
        outbox_batch_size: Outbox 事件单轮派发的批大小。
        outbox_max_attempts: Outbox 事件的最大尝试次数，超过后事件转入死信状态。
        api_p95_target_ms: API p95 延迟 SLO 目标（毫秒），用于请求完成日志的 SLO 判定。
        context_input_limit: 单次模型调用的上下文输入 token 预算上限。
        context_reserved_output: 上下文预算中为模型输出预留的 token 数。
        context_system_policy_reserve: 上下文预算中为系统策略文本预留的 token 数。
        context_tool_schema_reserve: 上下文预算中为工具 schema 预留的 token 数。
        context_safety_margin: 上下文预算的安全边际，用于吸收 token 估算误差。
        summary_segment_messages: 分层摘要中单个片段覆盖的消息条数。
        summary_hierarchy_segments: 高层摘要聚合低层片段的数量。
        memory_recall_tokens: 记忆召回内容允许占用的 token 预算。
        memory_recall_limit: 单次记忆召回的条数上限。
        memory_auto_commit_low_risk_preferences: 是否自动提交低风险偏好类记忆（不等待人工确认）。

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
        """配置加载后的整体校验：执行生产安全基线与通用约束。

        使用场景：pydantic-settings 构造实例时自动触发；任何违规都会抛出
        ``ValueError`` 使启动失败（fail fast），避免带病运行。

        Returns:
            校验通过的配置实例本身。

        Raises:
            ValueError: 生产环境违反安全基线，或通用约束（算法、加密配置）非法。

        """
        # 1. 生产环境基线：禁用调试输出与离线模型。
        if self.environment is Environment.PRODUCTION and self.debug_full_io:
            raise ValueError("debug_full_io must be disabled in production")
        if self.environment is Environment.PRODUCTION and self.offline_model:
            raise ValueError("offline_model is only valid for development and tests")
        # 2. 生产环境认证基线：禁用开发令牌，强制完整且仅 HTTPS 的 OIDC 配置。
        if self.environment is Environment.PRODUCTION:
            if self.bff_auth_token is not None:
                raise ValueError(
                    "bff_auth_token is a development adapter and is forbidden in production"
                )
            oidc_values = (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
            if not all(oidc_values):
                raise ValueError("oidc_issuer, oidc_audience and oidc_jwks_url are required")
            # 拒绝 HS 系对称算法与 none，防止 JWT 伪造。
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
            # 3. 生产环境服务间与持久化基线：强制服务令牌、PostgreSQL、Alembic 迁移。
            if self.agent_server_service_token is None:
                raise ValueError("agent_server_service_token is required in production")
            database_url = self.database_url.get_secret_value()
            if not database_url.startswith(("postgresql+psycopg://", "postgresql://")):
                raise ValueError("production database_url must use PostgreSQL")
            if self.database_auto_create_schema:
                raise ValueError("database_auto_create_schema must be disabled in production")
            # 4. 生产环境存储与观测基线：S3 产物桶、双 OTel 端点、低采样并隐藏输入输出。
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

        # 5. 通用约束：OIDC 仅允许非对称算法；S3 加密算法合法且 KMS 密钥与算法配套。
        allowed_algorithms = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not set(self.oidc_algorithms).issubset(allowed_algorithms):
            raise ValueError("oidc_algorithms contains an unsupported or symmetric algorithm")
        if self.artifact_s3_sse_algorithm not in {"AES256", "aws:kms", "aws:kms:dsse"}:
            raise ValueError("unsupported S3 server-side encryption algorithm")
        if self.artifact_s3_kms_key_id and not self.artifact_s3_sse_algorithm.startswith("aws:kms"):
            raise ValueError("artifact_s3_kms_key_id requires aws:kms encryption")
        return self
