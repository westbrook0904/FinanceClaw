"""`test_security_hardening` 模块提供`stage5`相关能力。"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr, ValidationError

from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.security import EgressDenied, EgressPolicy
from financeclaw.interfaces.http.auth import OIDCJWTAuthenticator


@pytest.mark.asyncio
async def test_oidc_verifier_projects_trusted_claims_and_rejects_tampering() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 private_key，供后续步骤使用。
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    # 准备 now，供后续步骤使用。
    now = datetime.now(UTC)
    # 准备 claims，供后续步骤使用。
    claims = {
        "iss": "https://id.example.test/",
        "aud": "financeclaw-api",
        "sub": "subject-a",
        "tenant_id": "tenant-a",
        "scope": "market:read artifacts:read",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    # 准备 token，供后续步骤使用。
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})
    # 准备 authenticator，供后续步骤使用。
    authenticator = OIDCJWTAuthenticator(
        issuer="https://id.example.test/",
        audience="financeclaw-api",
        jwks_url="https://id.example.test/jwks.json",
        algorithms=("RS256",),
        signing_key_resolver=lambda _token: private_key.public_key(),
    )

    # 准备 principal，供后续步骤使用。
    principal = await authenticator.authenticate(token)

    # 继续执行前验证内部不变量。
    assert principal is not None
    # 继续执行前验证内部不变量。
    assert principal.tenant_id == "tenant-a"
    # 继续执行前验证内部不变量。
    assert principal.subject_id == "subject-a"
    # 继续执行前验证内部不变量。
    assert principal.scopes == {"market:read", "artifacts:read"}
    # 准备 wrong_audience，供后续步骤使用。
    wrong_audience = jwt.encode(
        {**claims, "aud": "other-api"},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    # 继续执行前验证内部不变量。
    assert await authenticator.authenticate(wrong_audience) is None


def test_production_settings_require_oidc_internal_auth_s3_and_telemetry() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ValidationError, match="oidc_issuer"):
        FinanceClawSettings(environment="production", debug_full_io=False)

    # 准备 settings，供后续步骤使用。
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

    # 继续执行前验证内部不变量。
    assert settings.bff_auth_token is None
    # 继续执行前验证内部不变量。
    assert settings.artifact_backend.value == "s3"


def test_egress_policy_rejects_userinfo_http_and_suffix_confusion() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    policy = EgressPolicy(frozenset({"api.example.com"}))

    assert policy.validate("https://api.example.com/v1") == "https://api.example.com/v1"
    with pytest.raises(EgressDenied):
        policy.validate("http://api.example.com/v1")
    with pytest.raises(EgressDenied):
        policy.validate("https://api.example.com.attacker.test/v1")
    with pytest.raises(EgressDenied):
        policy.validate("https://user:password@api.example.com/v1")
