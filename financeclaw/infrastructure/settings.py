"""Environment-driven settings with fail-closed production safety checks."""

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ArtifactBackend(StrEnum):
    """Supported large-object storage implementations."""

    LOCAL = "local"
    S3 = "s3"


class FinanceClawSettings(BaseSettings):
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
