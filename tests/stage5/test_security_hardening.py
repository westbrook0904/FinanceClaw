from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from financeclaw.api.auth import OIDCJWTAuthenticator
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.security import EgressDenied, EgressPolicy


@pytest.mark.asyncio
async def test_oidc_verifier_projects_trusted_claims_and_rejects_tampering() -> None:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    now = datetime.now(UTC)
    claims = {
        "iss": "https://id.example.test/",
        "aud": "financeclaw-api",
        "sub": "subject-a",
        "tenant_id": "tenant-a",
        "scope": "market:read artifacts:read",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})
    authenticator = OIDCJWTAuthenticator(
        issuer="https://id.example.test/",
        audience="financeclaw-api",
        jwks_url="https://id.example.test/jwks.json",
        algorithms=("RS256",),
        signing_key_resolver=lambda _token: private_key.public_key(),
    )

    principal = await authenticator.authenticate(token)

    assert principal is not None
    assert principal.tenant_id == "tenant-a"
    assert principal.subject_id == "subject-a"
    assert principal.scopes == {"market:read", "artifacts:read"}
    wrong_audience = jwt.encode(
        {**claims, "aud": "other-api"},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    assert await authenticator.authenticate(wrong_audience) is None


def test_production_settings_require_oidc_internal_auth_s3_and_telemetry() -> None:
    with pytest.raises(ValidationError, match="oidc_issuer"):
        FinanceClawSettings(environment="production", debug_full_io=False)

    settings = FinanceClawSettings(
        environment="production",
        debug_full_io=False,
        oidc_issuer="https://id.example.test/",
        oidc_audience="financeclaw-api",
        oidc_jwks_url="https://id.example.test/jwks.json",
        oidc_algorithms=("RS256",),
        agent_server_service_token=SecretStr("service-secret"),
        database_url=SecretStr("postgresql+psycopg://user:secret@db/financeclaw_app"),
        database_auto_create_schema=False,
        artifact_backend="s3",
        artifact_s3_bucket="financeclaw-artifacts",
        otel_exporter_endpoint="https://otel.example.test/v1/traces",
        otel_metrics_exporter_endpoint="https://otel.example.test/v1/metrics",
        langsmith_trace_sample_rate=0.05,
        langsmith_hide_inputs=True,
        langsmith_hide_outputs=True,
        egress_allowed_hosts={
            "api.deepseek.com",
            "id.example.test",
            "api.smith.langchain.com",
            "otel.example.test",
        },
    )

    assert settings.bff_auth_token is None
    assert settings.artifact_backend.value == "s3"


def test_egress_policy_rejects_userinfo_http_and_suffix_confusion() -> None:
    policy = EgressPolicy(frozenset({"api.example.com"}))

    assert policy.validate("https://api.example.com/v1") == "https://api.example.com/v1"
    with pytest.raises(EgressDenied):
        policy.validate("http://api.example.com/v1")
    with pytest.raises(EgressDenied):
        policy.validate("https://api.example.com.attacker.test/v1")
    with pytest.raises(EgressDenied):
        policy.validate("https://user:password@api.example.com/v1")
