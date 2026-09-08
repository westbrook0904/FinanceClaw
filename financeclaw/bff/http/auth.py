"""HTTP 认证适配：把 Bearer 凭据校验为可信任的调用方身份。

本模块属于 interfaces（HTTP 协议适配层）。生产 BFF 使用 OIDC/JWT，
校验 issuer、audience、时效与非对称算法，并从可信 claims 生成
tenant、subject 与 scopes；静态 token 仅允许本地开发使用。
"""

import asyncio
from collections.abc import Callable, Mapping
from hmac import compare_digest
from typing import Annotated, Any, Protocol

import jwt
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedPrincipal(BaseModel):
    """认证成功后的调用方身份：租户、主体与权限范围的三元组。

    使用场景：由各 ``Authenticator`` 实现从凭据中提取并返回，FastAPI
    依赖据此向应用层传递租户隔离与鉴权所需的身份信息。

    Attributes:
        tenant_id: 租户 ID，用于多租户数据隔离。
        subject_id: 主体（用户或服务身份）ID，用于归属判定与审计。
        scopes: 权限范围集合，默认为空集，表示未授予任何范围。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    subject_id: str
    scopes: frozenset[str] = Field(default_factory=frozenset)


class Authenticator(Protocol):
    """认证器协议：把 Bearer token 校验为调用方身份的统一抽象。

    使用场景：``create_app`` 依赖本协议注入认证实现，使接口层不绑定
    具体机制，生产 OIDC/JWT 与本地静态 token 认证器可互换。
    """

    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """校验 Bearer token 并返回调用方身份。

        Args:
            bearer_token: 从 ``Authorization: Bearer`` 头提取的裸 token。

        Returns:
            校验成功时返回调用方身份；凭据无效或过期时返回 None。

        """
        ...


class StaticBearerAuthenticator:
    """静态 token 认证器：在预置的 token 到身份映射表中做常量时间查找。

    使用场景：仅限本地开发与测试环境，把配置的 ``bff_auth_token``
    映射为固定身份，免去搭建 OIDC 提供方的成本；生产环境禁止使用。

    Attributes:
        _principals: （私有）静态 token 到 ``AuthenticatedPrincipal`` 的映射表。

    """

    def __init__(self, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
        """装配静态 token 到调用方身份的映射表。

        Args:
            principals: token（键）到调用方身份（值）的映射。

        """
        self._principals = dict(principals)

    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """以常量时间比较在映射表中查找 token 对应的身份。

        Args:
            bearer_token: 待校验的裸 token。

        Returns:
            匹配成功时返回预置身份；未匹配时返回 None。

        """
        for expected, principal in self._principals.items():
            if compare_digest(bearer_token, expected):
                return principal
        return None


class OIDCJWTAuthenticator:
    """OIDC/JWT 认证器：生产 BFF 的默认认证实现。

    使用场景：``oidc_issuer``/``oidc_audience``/``oidc_jwks_url`` 配置
    齐备时启用；校验签名、issuer、audience 与时效，算法仅允许显式
    声明的非对称白名单（RS*、ES*），并从可信 claims 生成调用方身份。

    Attributes:
        _issuer: （私有）期望的令牌签发方（``iss`` claim 须等于该值）。
        _audience: （私有）期望的受众（``aud`` claim 须包含该值）。
        _algorithms: （私有）允许的非对称签名算法白名单。
        _tenant_claim: （私有）承载租户 ID 的 claim 名。
        _subject_claim: （私有）承载主体 ID 的 claim 名。
        _scope_claim: （私有）承载权限范围的 claim 名。
        _leeway_seconds: （私有）时效校验允许的时钟偏移容忍秒数。
        _jwks: （私有）内置 JWKS 客户端；注入自定义密钥解析器时为 None。
        _resolve_key: （私有）由 JWT 解析出验签公钥的密钥解析器。

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
        """装配 OIDC/JWT 校验参数，并构建验签公钥解析器。

        Args:
            issuer: 期望的签发方（``iss`` claim 须等于该值）。
            audience: 期望的受众（``aud`` claim 须包含该值）。
            jwks_url: OIDC 提供方的 JWKS 端点，用于获取验签公钥。
            algorithms: 允许的非对称签名算法白名单，默认 RS256 与 ES256。
            tenant_claim: 承载租户 ID 的 claim 名，默认 ``tenant_id``。
            subject_claim: 承载主体 ID 的 claim 名，默认 ``sub``。
            scope_claim: 承载权限范围的 claim 名，默认 ``scope``。
            leeway_seconds: 时效校验的时钟偏移容忍秒数，默认 30。
            jwks_timeout_seconds: 拉取 JWKS 的超时秒数，默认 5.0。
            signing_key_resolver: 可选的自定义签名密钥解析器；注入后
                不再创建内置 JWKS 客户端（便于测试替换）。

        Raises:
            ValueError: ``algorithms`` 为空，或包含白名单之外（含对称
                HS* 与 none）的算法。

        """
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
        """在线程池中执行同步 JWT 校验，避免阻塞事件循环。

        Args:
            bearer_token: 待校验的裸 JWT。

        Returns:
            校验通过时返回由可信 claims 构建的身份；否则返回 None。

        """
        return await asyncio.to_thread(self._authenticate_sync, bearer_token)

    def _authenticate_sync(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        """同步校验 JWT，并从可信 claims 生成调用方身份。

        Args:
            bearer_token: 待校验的裸 JWT。

        Returns:
            校验通过时返回 ``AuthenticatedPrincipal``；任何校验失败
            （算法越界、签名/时效/aud/iss 无效、claims 类型不符等）
            统一返回 None，不向外泄露失败细节。

        """
        try:
            # 1. 预检 JOSE 头：算法必须在白名单内且携带 kid，防算法混淆攻击。
            header = jwt.get_unverified_header(bearer_token)
            if header.get("alg") not in self._algorithms or not header.get("kid"):
                return None
            # 2. 解析验签公钥并完整校验签名、issuer、audience 与时效（含偏移容忍）。
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
            # 3. 提取租户与主体：两者必须都是字符串，才视为可信身份。
            tenant_id = claims.get(self._tenant_claim)
            subject_id = claims.get(self._subject_claim)
            if not isinstance(tenant_id, str) or not isinstance(subject_id, str):
                return None
            # 4. 解析权限范围：兼容空格分隔字符串与字符串集合两种 claim 形态。
            raw_scopes = claims.get(self._scope_claim, ())
            if isinstance(raw_scopes, str):
                scopes = frozenset(raw_scopes.split())
            elif isinstance(raw_scopes, (list, tuple, set)):
                scopes = frozenset(value for value in raw_scopes if isinstance(value, str))
            else:
                return None
            # 5. 用可信 claims 构建调用方身份。
            return AuthenticatedPrincipal(
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
            )
        except (jwt.InvalidTokenError, ValueError, TypeError):
            # 任何校验失败都统一退化为认证不通过。
            return None


def principal_dependency(authenticator: Authenticator):
    """构造 FastAPI 认证依赖：从请求头提取 Bearer 凭据并完成认证。

    Args:
        authenticator: 实际执行凭据校验的认证器实现。

    Returns:
        依赖函数 ``resolve_principal``，供各路由经 ``Depends`` 注入
        调用方身份。

    """

    async def resolve_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        """从 ``Authorization`` 头解析 Bearer token 并返回调用方身份。

        Args:
            authorization: 原始 ``Authorization`` 请求头，可为 None。

        Returns:
            认证成功后的调用方身份。

        Raises:
            HTTPException: 缺少 Bearer 凭据或凭据无效时抛出 401。

        """
        # 1. 按 ``<scheme> <token>`` 拆分请求头，scheme 须为 bearer 且 token 非空。
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="valid bearer authentication is required")
        # 2. 委派认证器校验 token；失败统一 401，不区分具体失败原因。
        principal = await authenticator.authenticate(token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid bearer credential")
        return principal

    return resolve_principal
