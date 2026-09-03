"""统一错误映射：把应用层与模块层异常翻译为稳定错误码的 HTTP 响应。

本模块属于 interfaces（HTTP 协议适配层），通过 FastAPI 异常处理器把
业务异常统一映射为 ``ErrorResponse`` + 对应状态码，路由与业务服务
无须各自处理错误序列化。
"""

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
    """向应用注册统一异常处理器，建立业务异常到 HTTP 错误的映射。

    映射规则：资源不存在类映射 404；幂等冲突与会话/流程状态冲突类
    映射 409；审批过期类映射 410；权限不足类映射 403；输入不合法类
    映射 422。响应体统一使用 ``ErrorResponse``（code + message）。

    Args:
        app: 待安装错误处理器的 FastAPI 应用。

    """

    @app.exception_handler(TargetResolutionError)
    async def target_error(_request: Request, exc: TargetResolutionError) -> JSONResponse:
        """目标解析失败（目录中无此工具/流程/Agent）映射为 404。"""
        payload = ErrorResponse(code="TARGET_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(RunNotFound)
    async def run_error(_request: Request, exc: RunNotFound) -> JSONResponse:
        """运行不存在映射为 404 RUN_NOT_FOUND。"""
        payload = ErrorResponse(code="RUN_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_error(_request: Request, exc: IdempotencyConflict) -> JSONResponse:
        """同键请求但内容不一致的幂等冲突映射为 409。"""
        payload = ErrorResponse(code="IDEMPOTENCY_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(ConversationNotFound)
    async def conversation_error(_request: Request, exc: ConversationNotFound) -> JSONResponse:
        """会话不存在映射为 404 CONVERSATION_NOT_FOUND。"""
        payload = ErrorResponse(code="CONVERSATION_NOT_FOUND", message=str(exc))
        return JSONResponse(status_code=404, content=payload.model_dump(mode="json"))

    @app.exception_handler(ConversationConflict)
    async def conversation_conflict(_request: Request, exc: ConversationConflict) -> JSONResponse:
        """会话状态冲突（如对已关闭会话发言）映射为 409。"""
        payload = ErrorResponse(code="CONVERSATION_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(ApprovalExpired)
    async def approval_expired(_request: Request, exc: ApprovalExpired) -> JSONResponse:
        """审批窗口已过期映射为 410 APPROVAL_EXPIRED。"""
        payload = ErrorResponse(code="APPROVAL_EXPIRED", message=str(exc))
        return JSONResponse(status_code=410, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowInputError)
    async def workflow_input(_request: Request, exc: WorkflowInputError) -> JSONResponse:
        """Workflow 入参不合法映射为 422 WORKFLOW_INPUT_INVALID。"""
        payload = ErrorResponse(code="WORKFLOW_INPUT_INVALID", message=str(exc))
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowAuthorizationError)
    async def workflow_forbidden(
        _request: Request, exc: WorkflowAuthorizationError
    ) -> JSONResponse:
        """调用方无权操作该 Workflow 映射为 403 WORKFLOW_FORBIDDEN。"""
        payload = ErrorResponse(code="WORKFLOW_FORBIDDEN", message=str(exc))
        return JSONResponse(status_code=403, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowConflict)
    async def workflow_conflict(_request: Request, exc: WorkflowConflict) -> JSONResponse:
        """Workflow 状态冲突（如重复启动/未发布）映射为 409。"""
        payload = ErrorResponse(code="WORKFLOW_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.exception_handler(WorkflowApprovalExpired)
    async def workflow_approval_expired(
        _request: Request, exc: WorkflowApprovalExpired
    ) -> JSONResponse:
        """Workflow 审批窗口已过期映射为 410 WORKFLOW_APPROVAL_EXPIRED。"""
        payload = ErrorResponse(code="WORKFLOW_APPROVAL_EXPIRED", message=str(exc))
        return JSONResponse(status_code=410, content=payload.model_dump(mode="json"))

    @app.exception_handler(DelegationInputError)
    async def delegation_input(_request: Request, exc: DelegationInputError) -> JSONResponse:
        """委派入参不合法映射为 422 DELEGATION_INPUT_INVALID。"""
        payload = ErrorResponse(code="DELEGATION_INPUT_INVALID", message=str(exc))
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.exception_handler(DelegationAuthorizationError)
    async def delegation_forbidden(
        _request: Request, exc: DelegationAuthorizationError
    ) -> JSONResponse:
        """调用方无权发起该委派映射为 403 DELEGATION_FORBIDDEN。"""
        payload = ErrorResponse(code="DELEGATION_FORBIDDEN", message=str(exc))
        return JSONResponse(status_code=403, content=payload.model_dump(mode="json"))

    @app.exception_handler(DelegationConflict)
    async def delegation_conflict(_request: Request, exc: DelegationConflict) -> JSONResponse:
        """委派状态冲突（如重复恢复）映射为 409 DELEGATION_CONFLICT。"""
        payload = ErrorResponse(code="DELEGATION_CONFLICT", message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
