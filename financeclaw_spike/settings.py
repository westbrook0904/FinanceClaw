"""Configuration and production safety checks for the Stage-0 spike."""

from enum import StrEnum

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class SpikeSettings(BaseSettings):
    """Settings are environment-driven so Agent Server can construct the graph."""

    model_config = SettingsConfigDict(
        env_prefix="FINANCECLAW_SPIKE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    model: str = "openai:gpt-5.4-mini"
    fallback_model: str | None = None
    offline_model: bool = False
    debug_full_io: bool = True
    read_max_retries: int = Field(default=2, ge=0, le=8)
    read_retry_initial_delay: float = Field(default=0.05, ge=0, le=10)
    mcp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    postgres_dsn: SecretStr | None = None
    redis_url: SecretStr | None = None
    langsmith_project: str = "financeclaw-stage0-development"

    @model_validator(mode="after")
    def protect_production_io(self) -> "SpikeSettings":
        if self.environment is Environment.PRODUCTION and self.debug_full_io:
            raise ValueError("debug_full_io must be disabled in production")
        if self.environment is Environment.PRODUCTION and self.offline_model:
            raise ValueError("offline_model is only valid for development and tests")
        return self
