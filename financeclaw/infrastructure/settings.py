"""Environment-driven settings with production safety checks."""

from enum import StrEnum

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


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
    model_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    model_max_tokens: int = Field(default=4096, ge=64, le=384_000)
    model_max_retries: int = Field(default=2, ge=0, le=8)
    read_max_attempts: int = Field(default=3, ge=1, le=8)
    mcp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    agent_server_url: str = "http://127.0.0.1:2024"
    agent_server_service_token: SecretStr | None = None
    bff_auth_token: SecretStr | None = None
    bff_tenant_id: str = "development"
    bff_subject_id: str = "developer"
    bff_scopes: frozenset[str] = Field(
        default_factory=lambda: frozenset({"market:read", "tools:read", "watchlist:write"})
    )
    langsmith_project: str = "financeclaw-stage1-development"

    @model_validator(mode="after")
    def protect_production(self) -> "FinanceClawSettings":
        if self.environment is Environment.PRODUCTION and self.debug_full_io:
            raise ValueError("debug_full_io must be disabled in production")
        if self.environment is Environment.PRODUCTION and self.offline_model:
            raise ValueError("offline_model is only valid for development and tests")
        if self.environment is Environment.PRODUCTION and self.bff_auth_token is None:
            raise ValueError("bff_auth_token is required in production")
        return self
