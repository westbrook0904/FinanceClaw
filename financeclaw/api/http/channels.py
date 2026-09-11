"""Authenticated, bounded internal ingress; identities/scopes come from server configuration."""

from hmac import compare_digest
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from financeclaw.shared.channels.feishu.contracts import FeishuInboundMessage


class ChannelEvent(BaseModel):
    """Accept exactly one normalized channel message or verified card callback."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["message", "card"]
    message: FeishuInboundMessage | None = None
    event: dict | None = None


class ReplyCollector:
    """Collect immediate channel replies for the integrations process to deliver."""

    def __init__(self):
        """Inject dependencies without starting background work."""
        self.replies = []

    async def send_text(self, **kwargs):
        """Collect a reply without sending a network message from the API process."""
        self.replies.append(kwargs)
        return True


def channel_router(service, settings):
    """Protect normalized channel ingress with the integrations service credential."""
    router = APIRouter()

    @router.post("/internal/channels/feishu/events")
    async def accept(request: ChannelEvent, authorization: Annotated[str | None, Header()] = None):
        """Authenticate internal ingress and dispatch one normalized channel event."""
        expected = settings.integration_service_token
        if (
            expected is None
            or not authorization
            or not compare_digest(authorization, "Bearer " + expected.get_secret_value())
        ):
            raise HTTPException(status_code=401, detail="integration authentication required")
        if request.kind == "card":
            if request.event is None or request.message is not None:
                raise HTTPException(status_code=422, detail="one card event is required")
            return await service.card_actions.handle(request.event)
        if request.message is None or request.event is not None:
            raise HTTPException(status_code=422, detail="one normalized message is required")
        collector = ReplyCollector()
        status = await service.process(request.message, collector)
        return {"status": status, "replies": collector.replies}

    return router
