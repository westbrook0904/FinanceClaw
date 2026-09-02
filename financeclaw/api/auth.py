"""Authentication boundary that maps verified credentials to trusted principals."""

from collections.abc import Mapping
from hmac import compare_digest
from typing import Annotated, Protocol

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
