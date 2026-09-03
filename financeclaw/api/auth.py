"""Authentication boundary that maps verified credentials to trusted principals."""

import asyncio
from collections.abc import Callable, Mapping
from hmac import compare_digest
from typing import Annotated, Any, Protocol

import jwt
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    subject_id: str
    scopes: frozenset[str] = Field(default_factory=frozenset)


class Authenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None: ...


class StaticBearerAuthenticator:
    """Development/test adapter; production composition should inject an OIDC verifier."""

    def __init__(self, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
        self._principals = dict(principals)

    async def authenticate(self, bearer_token: str) -> AuthenticatedPrincipal | None:
        for expected, principal in self._principals.items():
            if compare_digest(bearer_token, expected):
                return principal
        return None


class OIDCJWTAuthenticator:
    """Verify an asymmetric OIDC access token and project only trusted identity claims."""

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
        return await asyncio.to_thread(self._authenticate_sync, bearer_token)

    def _authenticate_sync(self, bearer_token: str) -> AuthenticatedPrincipal | None:
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
    async def resolve_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="valid bearer authentication is required")
        principal = await authenticator.authenticate(token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid bearer credential")
        return principal

    return resolve_principal
