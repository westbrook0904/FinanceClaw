"""Stable HTTP errors, with no native state or remote exception payloads."""

from fastapi.responses import JSONResponse

from financeclaw.shared.conversation.repository import (
    ConversationConflict,
    ConversationNotFound,
    IdempotencyConflict,
)
from financeclaw.shared.turns.types import (
    ExecutionConflict,
    InteractionConflict,
    InteractionNotFound,
    TurnNotFound,
)


def install_error_handlers(app):
    """Map business conflicts and ownership failures to bounded HTTP errors."""
    mapping = {
        ConversationNotFound: (404, "CONVERSATION_NOT_FOUND"),
        TurnNotFound: (404, "TURN_NOT_FOUND"),
        InteractionNotFound: (404, "INTERACTION_NOT_FOUND"),
        IdempotencyConflict: (409, "IDEMPOTENCY_CONFLICT"),
        ConversationConflict: (409, "CONVERSATION_CONFLICT"),
        InteractionConflict: (409, "INTERACTION_CONFLICT"),
        ExecutionConflict: (409, "EXECUTION_CONFLICT"),
        PermissionError: (403, "AUTHORIZATION_DENIED"),
    }

    def handler(status, code):
        """Bind a stable HTTP status to one business exception category."""

        async def respond(request, exc):
            """Return the bounded public error without leaking native state or database details."""
            return JSONResponse(
                status_code=status,
                content={
                    "code": code,
                    "message": "required authorization is missing" if status == 403 else str(exc),
                },
            )

        return respond

    for error, (status, code) in mapping.items():
        app.add_exception_handler(error, handler(status, code))
