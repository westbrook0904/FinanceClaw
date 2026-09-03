"""Stable API error projection."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from financeclaw.application import (
    ApprovalExpired,
    IdempotencyConflict,
    RunNotFound,
    TargetResolutionError,
    WorkflowApprovalExpired,
    WorkflowAuthorizationError,
    WorkflowInputError,
)
from financeclaw.contracts import ErrorResponse
from financeclaw.conversation import ConversationConflict, ConversationNotFound
from financeclaw.workflows import WorkflowConflict


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TargetResolutionError)
    async def target_error(_request: Request, exc: TargetResolutionError) -> JSONResponse:
        payload = ErrorResponse(code="TARGET_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(RunNotFound)
    async def run_error(_request: Request, exc: RunNotFound) -> JSONResponse:
        payload = ErrorResponse(code="RUN_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_error(_request: Request, exc: IdempotencyConflict) -> JSONResponse:
        payload = ErrorResponse(code="IDEMPOTENCY_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(ConversationNotFound)
    async def conversation_error(_request: Request, exc: ConversationNotFound) -> JSONResponse:
        payload = ErrorResponse(code="CONVERSATION_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(ConversationConflict)
    async def conversation_conflict(_request: Request, exc: ConversationConflict) -> JSONResponse:
        payload = ErrorResponse(code="CONVERSATION_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(ApprovalExpired)
    async def approval_expired(_request: Request, exc: ApprovalExpired) -> JSONResponse:
        payload = ErrorResponse(code="APPROVAL_EXPIRED", message=str(exc))
        return JSONResponse(status_code=410, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowInputError)
    async def workflow_input(_request: Request, exc: WorkflowInputError) -> JSONResponse:
        payload = ErrorResponse(code="WORKFLOW_INPUT_INVALID", message=str(exc))
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowAuthorizationError)
    async def workflow_forbidden(
        _request: Request, exc: WorkflowAuthorizationError
    ) -> JSONResponse:
        payload = ErrorResponse(code="WORKFLOW_FORBIDDEN", message=str(exc))
        return JSONResponse(status_code=403, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowConflict)
    async def workflow_conflict(_request: Request, exc: WorkflowConflict) -> JSONResponse:
        payload = ErrorResponse(code="WORKFLOW_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowApprovalExpired)
    async def workflow_approval_expired(
        _request: Request, exc: WorkflowApprovalExpired
    ) -> JSONResponse:
        payload = ErrorResponse(code="WORKFLOW_APPROVAL_EXPIRED", message=str(exc))
        return JSONResponse(status_code=410, content=payload.model_dump(mode="json"))
