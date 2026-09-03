"""把领域及应用异常映射为稳定的 HTTP 错误响应。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from financeclaw.application import (
    ApprovalExpired,
    DelegationAuthorizationError,
    DelegationInputError,
    IdempotencyConflict,
    RunNotFound,
    TargetResolutionError,
    WorkflowApprovalExpired,
    WorkflowAuthorizationError,
    WorkflowInputError,
)
from financeclaw.kernel import ErrorResponse
from financeclaw.modules.conversation import ConversationConflict, ConversationNotFound
from financeclaw.modules.delegation import DelegationConflict
from financeclaw.modules.workflows import WorkflowConflict


def install_error_handlers(app: FastAPI) -> None:
    """把errors 模块的数据安装到目标运行时。"""

    @app.exception_handler(TargetResolutionError)
    async def target_error(_request: Request, exc: TargetResolutionError) -> JSONResponse:
        """将目标解析失败映射为 404 响应，并保留稳定错误码。"""
        payload = ErrorResponse(code="TARGET_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(RunNotFound)
    async def run_error(_request: Request, exc: RunNotFound) -> JSONResponse:
        """将运行不存在或不属于当前主体的情况映射为 404 响应。"""
        payload = ErrorResponse(code="RUN_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_error(_request: Request, exc: IdempotencyConflict) -> JSONResponse:
        """将幂等键与请求内容冲突映射为 409 响应。"""
        payload = ErrorResponse(code="IDEMPOTENCY_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(ConversationNotFound)
    async def conversation_error(_request: Request, exc: ConversationNotFound) -> JSONResponse:
        """将会话不存在或越权访问统一映射为 404 响应。"""
        payload = ErrorResponse(code="CONVERSATION_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(ConversationConflict)
    async def conversation_conflict(_request: Request, exc: ConversationConflict) -> JSONResponse:
        """将会话状态或幂等冲突映射为 409 响应。"""
        payload = ErrorResponse(code="CONVERSATION_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(ApprovalExpired)
    async def approval_expired(_request: Request, exc: ApprovalExpired) -> JSONResponse:
        """将已过审批期限的恢复请求映射为 409 响应。"""
        payload = ErrorResponse(code="APPROVAL_EXPIRED", message=str(exc))
        return JSONResponse(status_code=410, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowInputError)
    async def workflow_input(_request: Request, exc: WorkflowInputError) -> JSONResponse:
        """将工作流输入校验失败映射为 422 响应。"""
        payload = ErrorResponse(code="WORKFLOW_INPUT_INVALID", message=str(exc))
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowAuthorizationError)
    async def workflow_forbidden(
        _request: Request, exc: WorkflowAuthorizationError
    ) -> JSONResponse:
        """将工作流权限不足映射为 403 响应。"""
        payload = ErrorResponse(code="WORKFLOW_FORBIDDEN", message=str(exc))
        return JSONResponse(status_code=403, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowConflict)
    async def workflow_conflict(_request: Request, exc: WorkflowConflict) -> JSONResponse:
        """将工作流状态或审批冲突映射为 409 响应。"""
        payload = ErrorResponse(code="WORKFLOW_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowApprovalExpired)
    async def workflow_approval_expired(
        _request: Request, exc: WorkflowApprovalExpired
    ) -> JSONResponse:
        """将工作流审批过期映射为 409 响应。"""
        payload = ErrorResponse(code="WORKFLOW_APPROVAL_EXPIRED", message=str(exc))
        return JSONResponse(status_code=410, content=payload.model_dump(mode="json"))

    @app.exception_handler(DelegationInputError)
    async def delegation_input(_request: Request, exc: DelegationInputError) -> JSONResponse:
        """将委派目标或参数错误映射为 422 响应。"""
        payload = ErrorResponse(code="DELEGATION_INPUT_INVALID", message=str(exc))
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(DelegationAuthorizationError)
    async def delegation_forbidden(
        _request: Request, exc: DelegationAuthorizationError
    ) -> JSONResponse:
        """将委派权限不足映射为 403 响应。"""
        payload = ErrorResponse(code="DELEGATION_FORBIDDEN", message=str(exc))
        return JSONResponse(status_code=403, content=payload.model_dump(mode="json"))

    @app.exception_handler(DelegationConflict)
    async def delegation_conflict(_request: Request, exc: DelegationConflict) -> JSONResponse:
        """将委派状态机冲突映射为 409 响应。"""
        payload = ErrorResponse(code="DELEGATION_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
