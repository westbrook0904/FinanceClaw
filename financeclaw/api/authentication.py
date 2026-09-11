"""Build product authentication without importing HTTP routes or graph factories."""

from financeclaw.api.http.auth import (
    AuthenticatedPrincipal,
    OIDCJWTAuthenticator,
    StaticBearerAuthenticator,
)


def build_authenticator(settings):
    """Select production OIDC or the explicit development authentication adapter."""
    if settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
        return OIDCJWTAuthenticator(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            algorithms=settings.oidc_algorithms,
            tenant_claim=settings.oidc_tenant_claim,
            subject_claim=settings.oidc_subject_claim,
            scope_claim=settings.oidc_scope_claim,
            leeway_seconds=settings.oidc_clock_skew_seconds,
            jwks_timeout_seconds=settings.oidc_jwks_timeout_seconds,
        )
    principals = {}
    if settings.api_auth_token:
        principals[settings.api_auth_token.get_secret_value()] = AuthenticatedPrincipal(
            tenant_id=settings.api_tenant_id,
            subject_id=settings.api_subject_id,
            scopes=settings.api_scopes,
        )
    return StaticBearerAuthenticator(principals)
