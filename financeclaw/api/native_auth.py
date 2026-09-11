"""External native resources are private; integration maintenance has bounded Store access."""

from hmac import compare_digest

from langgraph_sdk import Auth

from financeclaw.shared.infrastructure.settings import FinanceClawSettings

auth = Auth()


@auth.authenticate
async def authenticate(authorization: str | None):
    """Loopback bypass is framework ASGI state, never a user-controlled header or URL."""
    settings = FinanceClawSettings()
    expected = settings.integration_service_token
    if (
        expected
        and authorization
        and compare_digest(authorization, "Bearer " + expected.get_secret_value())
    ):
        return {"identity": "financeclaw-integrations", "permissions": ["store:maintenance"]}
    # Product HTTP routes authenticate separately. No external product token owns native resources.
    raise Auth.exceptions.HTTPException(status_code=403, detail="native resources are private")


@auth.on
async def deny_native(ctx, value):
    """Reject external native operations unless a narrower resource handler allows them."""
    raise Auth.exceptions.HTTPException(status_code=403, detail="native execution is API-owned")


@auth.on.store
async def maintenance_store(ctx, value):
    """Permit history indexing and memory deletion, with a complete owner namespace."""
    namespace = value.get("namespace") or value.get("namespace_prefix") or ()
    if (
        ctx.user.identity != "financeclaw-integrations"
        or "store:maintenance" not in ctx.permissions
        or not isinstance(namespace, (list, tuple))
        or len(namespace) not in {5, 6}
        or tuple(namespace[:2]) != ("financeclaw", "v2")
        or any(not isinstance(part, str) or not part or "*" in part for part in namespace)
    ):
        raise Auth.exceptions.HTTPException(
            status_code=403, detail="bounded maintenance namespace required"
        )
    category = namespace[4]
    if (
        category == "history"
        and len(namespace) == 6
        and ctx.action in {"get", "put", "delete", "search"}
    ):
        return True
    if (
        category in {"events", "profile"}
        and len(namespace) == 5
        and ctx.action in {"get", "delete"}
    ):
        return True
    raise Auth.exceptions.HTTPException(
        status_code=403, detail="maintenance action is not permitted"
    )
