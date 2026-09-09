"""统一错误映射：把应用层与模块层异常翻译为稳定错误码的 HTTP 响应。

本模块属于 interfaces（HTTP 协议适配层），通过 FastAPI 异常处理器把
业务异常统一映射为 ``ErrorResponse`` + 对应状态码，路由与业务服务
无须各自处理错误序列化。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from financeclaw.kernel.responses import ErrorResponse
from financeclaw.kernel.run_errors import IdempotencyConflict, RunNotFound
from financeclaw.shared.conversation.repository import ConversationConflict, ConversationNotFound
from financeclaw.shared.execution_ledger.interactions import InteractionNotFound
from financeclaw.shared.execution_ledger.repository import ExecutionConflict


def install_error_handlers(app: FastAPI) -> None:
    """向应用注册统一异常处理器，建立业务异常到 HTTP 错误的映射。

    映射规则：资源不存在类映射 404；幂等冲突与会话/流程状态冲突类
    映射 409；审批过期类映射 410；权限不足类映射 403；输入不合法类
    映射 422。响应体统一使用 ``ErrorResponse``（code + message）。

    Args:
        app: 待安装错误处理器的 FastAPI 应用。

    """

    @app.exception_handler(InteractionNotFound)
    async def interaction_not_found(_request: Request, exc: InteractionNotFound) -> JSONResponse:
        """交互不存在与归属不匹配统一返回 404，不暴露其他主体的状态。"""
        return JSONResponse(
            status_code=404, content={"code": "INTERACTION_NOT_FOUND", "message": str(exc)}
        )

    @app.exception_handler(ExecutionConflict)
    async def execution_conflict(_request: Request, exc: ExecutionConflict) -> JSONResponse:
        """快照缺失、提交不确定或旧版本不可恢复时返回可定位的冲突。"""
        return JSONResponse(
            status_code=409, content={"code": "EXECUTION_CONFLICT", "message": str(exc)}
        )

    @app.exception_handler(PermissionError)
    async def authorization_denied(_request: Request, _exc: PermissionError) -> JSONResponse:
        """后台受理缺少执行或交互权限时拒绝，不输出任何请求载荷。"""
        return JSONResponse(
            status_code=403,
            content={
                "code": "AUTHORIZATION_DENIED",
                "message": "required authorization is missing",
            },
        )

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
