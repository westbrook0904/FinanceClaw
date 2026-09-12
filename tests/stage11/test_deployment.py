"""Dedicated memory process credentials, deployment composition and production policy."""

from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from deploy.memory_worker_entrypoint import memory_database_url
from deploy.provision_memory_role import _expected_privileges
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


def test_memory_database_url_escapes_password_and_fixes_principal():
    """Preserve reserved characters without accepting an administrative connection override."""
    password = "synthetic-memory-password@:/%?= +汉字"
    url = make_url(
        memory_database_url(
            {
                "MEMORY_POSTGRES_PASSWORD": password,
                "FINANCECLAW_DATABASE_URL": "postgresql://admin@elsewhere/other",
                "MEMORY_POSTGRES_HOST": "memory-db",
            }
        )
    )
    assert url.password == password
    assert url.username == "financeclaw_memory" and url.host == "memory-db"
    assert url.database == "financeclaw_app"
    with pytest.raises(ValueError, match="at least 24"):
        memory_database_url({"MEMORY_POSTGRES_PASSWORD": "short"})


def test_compose_memory_role_does_not_inherit_application_secrets_or_artifacts():
    """Keep database admin, native Store, Redis and product credentials outside the new role."""
    services = yaml.safe_load(Path("compose.yml").read_text())["services"]
    worker = services["memory_worker"]
    assert worker["env_file"] == ["${FINANCECLAW_MEMORY_ENV_FILE:-.env.memory}"]
    assert worker["volumes"] == []
    assert worker["entrypoint"] == ["python", "deploy/memory_worker_entrypoint.py"]
    assert not {
        "POSTGRES_URI",
        "POSTGRES_PASSWORD",
        "REDIS_URI",
        "FINANCECLAW_DATABASE_URL",
        "FINANCECLAW_API_AUTH_TOKEN",
        "FINANCECLAW_INTEGRATION_SERVICE_TOKEN",
    }.intersection(worker["environment"])
    assert services["migrate"]["entrypoint"] == ["python", "deploy/migrate.py"]
    assert services["api"]["environment"]["N_JOBS_PER_WORKER"] == "0"
    assert _expected_privileges("conversation_turns") == {"SELECT"}
    assert _expected_privileges("turn_commands") == frozenset()
    assert _expected_privileges("interactions") == {"SELECT"}
    assert _expected_privileges("audit_records") == {"SELECT", "INSERT"}


@pytest.fixture
def production_memory_settings(monkeypatch):
    """Construct only the memory role's production dependencies with no inherited local env."""
    import os

    for key in tuple(os.environ):
        if key.startswith("FINANCECLAW_"):
            monkeypatch.delenv(key)
    return dict(
        _env_file=None,
        environment="production",
        process_role="memory_worker",
        database_url=SecretStr("postgresql+psycopg://financeclaw_memory@localhost/memory-test"),
        database_auto_create_schema=False,
        offline_model=False,
        debug_full_io=False,
        api_auth_token=None,
        integration_service_token=None,
        oidc_issuer=None,
        oidc_audience=None,
        oidc_jwks_url=None,
        artifact_backend="local",
        artifact_s3_bucket=None,
        langsmith_hide_inputs=True,
        langsmith_hide_outputs=True,
        langsmith_trace_sample_rate=0.1,
        otel_exporter_endpoint="https://telemetry.example/traces",
        otel_metrics_exporter_endpoint="https://telemetry.example/metrics",
    )


def test_production_memory_role_needs_no_product_or_artifact_credentials(
    production_memory_settings,
):
    """Allow the isolated role while the identical API configuration remains invalid."""
    settings = FinanceClawSettings(**production_memory_settings)
    assert settings.process_role == "memory_worker" and settings.api_auth_token is None
    with pytest.raises(ValidationError, match="oidc_issuer"):
        FinanceClawSettings(**{**production_memory_settings, "process_role": "api"})


@pytest.mark.parametrize(
    "invalid",
    [
        {"database_url": SecretStr("sqlite:///unsupported.db")},
        {"database_auto_create_schema": True},
        {"offline_model": True},
        {"debug_full_io": True},
        {"langsmith_hide_inputs": False},
    ],
)
def test_production_memory_role_keeps_data_and_privacy_requirements(
    production_memory_settings, invalid
):
    """Removing irrelevant role credentials must not weaken memory data protection."""
    with pytest.raises(ValidationError):
        FinanceClawSettings(**{**production_memory_settings, **invalid})
