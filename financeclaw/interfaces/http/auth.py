"""解析 Bearer 凭证并构造带租户、主体和权限域的身份。"""

import asyncio
from collections.abc import Callable, Mapping
from hmac import compare_digest
from typing import Annotated, Any, Protocol

import jwt
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedPrincipal(BaseModel):
    """定义已认证主体。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        scopes: 调用主体拥有的权限域集合。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    subject_id: str
    scopes: frozenset[str] = Field(default_factory=frozenset)


class Authenticator(Protocol):
    """定义Authenticator。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """验证 Bearer 凭证，并返回包含租户、主体和权限域的可信身份。"""
        ...


class StaticBearerAuthenticator:
    """定义StaticBearerAuthenticator。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        _principals: 内部 `principals` 状态或依赖，不属于公开接口。
    """

    def __init__(self, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
        """注入并保存StaticBearerAuthenticator所需的协作对象，同时校验构造期不变量。"""
        self._principals = dict(principals)

    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """验证 Bearer 凭证，并返回包含租户、主体和权限域的可信身份。"""
        for expected, principal in self._principals.items():
            if compare_digest(bearer_token, expected):
                return principal
        return None


class OIDCJWTAuthenticator:
    """定义OIDCJWTAuthenticator。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        _issuer: 内部 `issuer` 状态或依赖，不属于公开接口。
        _audience: 内部 `audience` 状态或依赖，不属于公开接口。
        _algorithms: 内部 `algorithms` 状态或依赖，不属于公开接口。
        _tenant_claim: 内部 `tenant claim` 状态或依赖，不属于公开接口。
        _subject_claim: 内部 `subject claim` 状态或依赖，不属于公开接口。
        _scope_claim: 内部 `scope claim` 状态或依赖，不属于公开接口。
        _leeway_seconds: 该操作允许的最长时间（秒）。
        _jwks: 内部 `jwks` 状态或依赖，不属于公开接口。
        _resolve_key: 内部 `resolve key` 状态或依赖，不属于公开接口。
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: tuple[str, ...] = ("RS256", "ES256"),
        tenant_claim: str = "tenant_id",
        subject_claim: str = "sub",
        scope_claim: str = "scope",
        leeway_seconds: int = 30,
        jwks_timeout_seconds: float = 5.0,
        signing_key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        """注入并保存OIDCJWTAuthenticator所需的协作对象，同时校验构造期不变量。"""
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not algorithms or not set(algorithms).issubset(allowed):
            raise ValueError("OIDC algorithms must be an explicit asymmetric allowlist")
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms
        self._tenant_claim = tenant_claim
        self._subject_claim = subject_claim
        self._scope_claim = scope_claim
        self._leeway_seconds = leeway_seconds
        self._jwks = None
        if signing_key_resolver is None:
            self._jwks = jwt.PyJWKClient(jwks_url, timeout=jwks_timeout_seconds)
            signing_key_resolver = self._jwks.get_signing_key_from_jwt
        self._resolve_key = signing_key_resolver

    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """验证 Bearer 凭证，并返回包含租户、主体和权限域的可信身份。"""
        return await asyncio.to_thread(self._authenticate_sync, bearer_token)

    def _authenticate_sync(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """同步验证 JWT 签名、签发者、受众与必要 claim，再构造可信身份。"""
        try:
            header = jwt.get_unverified_header(bearer_token)
            if header.get("alg") not in self._algorithms or not header.get("kid"):
                return None
            claims = jwt.decode(
                bearer_token,
                self._resolve_key(bearer_token),
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud", self._subject_claim, self._tenant_claim]
                },
            )
            tenant_id = claims.get(self._tenant_claim)
            subject_id = claims.get(self._subject_claim)
            if not isinstance(tenant_id, str) or not isinstance(subject_id, str):
                return None
            raw_scopes = claims.get(self._scope_claim, ())
            if isinstance(raw_scopes, str):
                scopes = frozenset(raw_scopes.split())
            elif isinstance(raw_scopes, (list, tuple, set)):
                scopes = frozenset(value for value in raw_scopes if isinstance(value, str))
            else:
                return None
            return AuthenticatedPrincipal(
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
            )
        except (jwt.InvalidTokenError, ValueError, TypeError):
            return None


def principal_dependency(authenticator: Authenticator):
    """创建 FastAPI 身份依赖，从 Authorization 请求头提取并验证 Bearer 凭证。"""

    async def resolve_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        """解析并校验auth 模块的数据，返回固定版本的运行对象。"""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="valid bearer authentication is required")
        principal = await authenticator.authenticate(token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid bearer credential")
        return principal

    return resolve_principal
