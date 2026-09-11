"""Product routes: scoped Conversations, Turns and human decisions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from financeclaw.api.http.auth import principal_dependency
from financeclaw.api.http.streaming import project_sse
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.kernel.responses import ConversationTurnRequest, CreateConversationRequest
from financeclaw.shared.infrastructure.asyncio import run_sync


class AuthorizationRequest(BaseModel):
    """Require the authorization revision displayed to the HTTP caller."""

    model_config = ConfigDict(extra="forbid")
    expected_grant_revision: int


class CheckpointPruneRequest(BaseModel):
    """Require explicit apply and an enumerated native retention strategy."""

    model_config = ConfigDict(extra="forbid")
    apply: bool = False
    strategy: Literal["keep_latest", "delete"] = "keep_latest"


def product_router(conversations, turns, authenticator):
    """Register the canonical conversation, Turn and interaction routes."""
    router = APIRouter(prefix="/v1")
    principal = principal_dependency(authenticator)
    Principal = Annotated[object, Depends(principal)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)]

    def identity(user):
        """Extract only authenticated ownership fields."""
        return {"tenant_id": user.tenant_id, "subject_id": user.subject_id}

    async def owned(conversation_id, turn_id, user):
        """Check ownership and the conversation-to-Turn relationship before accessing state."""
        await run_sync(
            turns.store.assert_owned, turn_id, **identity(user), conversation_id=conversation_id
        )

    @router.post("/conversations", status_code=201)
    async def create(request: CreateConversationRequest, user: Principal):
        """Create a conversation without submitting a native graph execution."""
        return await conversations.create(**identity(user))

    @router.get("/conversations/{conversation_id}/messages")
    async def messages(
        conversation_id: str,
        user: Principal,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        """Read a bounded page of the owned conversation Journal."""
        return await run_sync(
            conversations.messages, conversation_id, **identity(user), after=after, limit=limit
        )

    @router.post("/conversations/{conversation_id}/turns", status_code=202)
    async def start(
        conversation_id: str, request: ConversationTurnRequest, user: Principal, key: Key
    ):
        """Accept a message-only task with an authenticated idempotency key."""
        return await turns.start_turn(
            conversation_id,
            request,
            **identity(user),
            scopes=user.scopes,
            idempotency_key=key,
            authorization=user.authorization,
        )

    base = "/conversations/{conversation_id}/turns/{turn_id}"

    @router.get(base)
    async def status(conversation_id: str, turn_id: str, user: Principal):
        """Read the current product snapshot without advancing execution."""
        await owned(conversation_id, turn_id, user)
        return await turns.status(turn_id, **identity(user))

    @router.get(base + "/events")
    async def events(
        conversation_id: str,
        turn_id: str,
        user: Principal,
        last_event_id: str | None = Header(default=None),
    ):
        """Subscribe to safe Turn snapshots independently of native execution."""
        await owned(conversation_id, turn_id, user)
        return StreamingResponse(
            project_sse(turns.stream(turn_id, **identity(user), last_event_id=last_event_id)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(base + "/cancel", status_code=202)
    async def cancel(conversation_id: str, turn_id: str, user: Principal, key: Key):
        """Persist cancellation intent; observation confirms when execution has stopped."""
        await owned(conversation_id, turn_id, user)
        return await turns.cancel(turn_id, **identity(user), command_id=key)

    @router.post(base + "/authorization", status_code=202)
    async def authorize(
        conversation_id: str, turn_id: str, request: AuthorizationRequest, user: Principal, key: Key
    ):
        """Record explicit finite authorization under the displayed revision."""
        await owned(conversation_id, turn_id, user)
        return await turns.reauthorize(
            turn_id,
            **identity(user),
            scopes=user.scopes,
            authorization=user.authorization,
            command_id=key,
            expected_grant_revision=request.expected_grant_revision,
        )

    @router.delete(base + "/authorization", status_code=202)
    async def revoke(
        conversation_id: str, turn_id: str, request: AuthorizationRequest, user: Principal, key: Key
    ):
        """Revoke future execution under the displayed authorization revision."""
        await owned(conversation_id, turn_id, user)
        return await turns.revoke_authorization(
            turn_id,
            **identity(user),
            command_id=key,
            expected_grant_revision=request.expected_grant_revision,
        )

    @router.get("/interactions/{interaction_id}")
    async def interaction(interaction_id: str, user: Principal):
        """Read the owned question without exposing native execution coordinates."""
        return await run_sync(turns.interactions.get_owned, interaction_id, **identity(user))

    @router.post("/interactions/{interaction_id}/responses", status_code=202)
    async def respond(interaction_id: str, request: InteractionResponse, user: Principal, key: Key):
        """Accept a typed human answer and return its committed product snapshot."""
        result = await turns.interactions.respond(
            interaction_id,
            request,
            **identity(user),
            scopes=user.scopes,
            idempotency_key=key,
            authorization=user.authorization,
        )
        return {
            "interaction": result,
            "turn": await turns.status(result["turn_id"], **identity(user)),
        }

    @router.post("/conversations/{conversation_id}/checkpoints/prune")
    async def prune(conversation_id: str, request: CheckpointPruneRequest, user: Principal):
        """Require product maintenance permission, separate from the integrations credential."""
        from financeclaw.shared.turns.authorization import require_scopes

        require_scopes(user.scopes, {"maintenance:checkpoints"})
        return await turns.maintenance.prune(
            conversation_id=conversation_id,
            **identity(user),
            apply=request.apply,
            strategy=request.strategy,
        )

    return router
