"""Authenticated BFF Webhook Ingress: persist wakeups, never trust callback results."""

import asyncio
import json
from hashlib import sha256
from hmac import compare_digest

from fastapi import APIRouter, HTTPException, Request, Response

from financeclaw.bff.application.runs.backend import native_id
from financeclaw.kernel.backend import BackendNotification
from financeclaw.shared.execution_ledger.root_repository import now


def webhook_router(store, settings):
    """Bind one fixed backend identity and secret to the BFF's persistent inbox."""
    router = APIRouter()

    @router.post("/internal/webhooks/langgraph/{backend_instance_id}", status_code=204)
    async def webhook(backend_instance_id: str, request: Request):
        """Authenticate before reading a bounded body and acknowledge only committed storage."""
        if settings.bff_webhook_token is None:
            raise HTTPException(404, "webhook is disabled")
        expected = "Bearer " + settings.bff_webhook_token.get_secret_value()
        if backend_instance_id != store.backend_instance_id or not compare_digest(
            request.headers.get("authorization", ""), expected
        ):
            raise HTTPException(401, "invalid backend authentication")
        return await persist(request, backend_instance_id)

    async def persist(request, backend_instance_id):
        """Share bounded parsing and commit-before-ack semantics between fixed routes."""
        if request.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(415, "content encoding is not supported")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "webhook exceeds body limit")
        try:
            payload = json.loads(body)
            if any(
                not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 128
                for key in ("thread_id", "run_id")
            ):
                raise ValueError("invalid native identity")
            notification = BackendNotification(
                backend_instance_id=backend_instance_id,
                execution_id=native_id(payload["thread_id"], payload["run_id"]),
                status_hint=payload["status"],
                payload_digest=sha256(body).hexdigest(),
                received_at=now(),
            )
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "invalid backend notification") from None
        try:
            await asyncio.to_thread(store.notify, notification)
        except Exception:
            raise HTTPException(503, "notification persistence unavailable") from None
        return Response(status_code=204)

    return router
