"""FastAPI BFF 应用装配：FinanceClaw 唯一的产品级 HTTP 写入口。

本模块属于 interfaces（HTTP 协议适配层）：只做输入校验、认证、错误
映射与 SSE 输出，业务规则一律委派 application 层服务，不复制实现。
"""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from financeclaw.application import (
    ConversationService,
    DelegationService,
    RunNotFound,
    RunService,
    TargetResolver,
    WorkflowInputError,
    WorkflowService,
)
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.clients import LangGraphAgentServerClient
from financeclaw.infrastructure.observability import (
    TelemetryRuntime,
    configure_json_logging,
    configure_langsmith,
    configure_telemetry,
    install_request_observability,
)
from financeclaw.kernel import (
    ApprovalDecision,
    ConversationMessagesResponse,
    ConversationResponse,
    ConversationTurnAccepted,
    ConversationTurnRequest,
    CreateConversationRequest,
    RunAccepted,
    RunRequest,
    RunStatusResponse,
    ToolInvokeRequest,
    ToolTarget,
    WorkflowInvokeRequest,
    WorkflowTarget,
)

from .auth import (
    AuthenticatedPrincipal,
    Authenticator,
    OIDCJWTAuthenticator,
    StaticBearerAuthenticator,
    principal_dependency,
)
from .errors import install_error_handlers
from .streaming import project_sse

# 模块级日志器：供 lifespan 记录启动补偿与关闭钩子的失败信息。
LOGGER = logging.getLogger(__name__)
# 内部服务身份专用权限范围：直连 Run/Workflow/Tool 路由要求该 scope。
_INTERNAL_INVOKE_SCOPE = "internal:invoke"


def _require_internal_invocation(principal: AuthenticatedPrincipal) -> None:
    """校验调用方持有内部调用权限，否则拒绝访问直连图执行入口。

    Args:
        principal: 当前请求已认证的调用方身份。

    Raises:
        HTTPException: 缺少 ``internal:invoke`` scope 时抛出 403。

    """
    if "*" not in principal.scopes and _INTERNAL_INVOKE_SCOPE not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail="direct graph invocation is restricted to internal service identities",
        )


def create_app(
    *,
    run_service: RunService,
    authenticator: Authenticator,
    conversation_service: ConversationService | None = None,
    workflow_service: WorkflowService | None = None,
    delegation_service: DelegationService | None = None,
    readiness_checks: Mapping[str, Callable[[], Awaitable[bool]]] | None = None,
    shutdown_hooks: tuple[Callable[[], Any], ...] = (),
    readiness_timeout_seconds: float = 3.0,
    shutdown_timeout_seconds: float = 20.0,
    p95_target_ms: int = 2_500,
) -> FastAPI:
    """装配 FastAPI BFF 应用：路由、认证、错误处理、观测与生命周期。

    使用场景：依赖注入式装配，测试与定制部署按需传入各应用服务；
    生产全量装配请使用 ``create_default_app``。

    Args:
        run_service: Run 编排服务，负责非会话直连运行的受理、状态与事件流。
        authenticator: 认证器，校验 Bearer 凭据并生成调用方身份。
        conversation_service: 可选会话服务；为 None 时不挂载会话相关路由。
        workflow_service: 可选 Workflow 服务；为 None 时不受理 Workflow 目标。
        delegation_service: 可选委派服务；为 None 时委派子运行状态不可查。
        readiness_checks: 就绪探针映射（名称到异步探针）；为 None 时仅探
            Agent Server 健康。
        shutdown_hooks: 关闭期回调元组，lifespan 中按注册逆序执行。
        readiness_timeout_seconds: 单个就绪探针的超时秒数。
        shutdown_timeout_seconds: 单个关闭钩子的超时秒数。
        p95_target_ms: 请求观测中间件统计 P95 延迟所用的目标毫秒数。

    Returns:
        配置好路由、错误处理、可观测性与 lifespan 的 FastAPI 应用。

    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """应用生命周期：启动时补偿未完成的持久化任务，关闭时执行钩子。

        Args:
            _app: FastAPI 应用实例（本实现未使用）。

        """
        # 1. 启动补偿：依次对 Workflow、委派与会话做未完成任务对账；
        #    失败仅记录日志并延后重试，不阻断进程启动。
        if workflow_service is not None:
            try:
                await workflow_service.reconcile_incomplete()
            except Exception:
                LOGGER.exception("workflow reconciliation deferred after startup failure")
        if delegation_service is not None:
            try:
                await delegation_service.reconcile_incomplete()
            except Exception:
                LOGGER.exception("delegation reconciliation deferred after startup failure")
        if conversation_service is not None:
            try:
                await conversation_service.reconcile_incomplete()
            except Exception:
                LOGGER.exception("conversation reconciliation deferred after startup failure")
        # 2. 让出控制权对外服务，直到进程关闭再进入 finally 收尾。
        try:
            yield
        finally:
            # 3. 关闭收尾：按注册逆序执行钩子（如先关数据库、最后 flush OTel）；
            #    协程钩子直接 await，普通函数放线程池，均受超时约束。
            for hook in reversed(shutdown_hooks):
                try:
                    if inspect.iscoroutinefunction(hook):
                        await asyncio.wait_for(hook(), timeout=shutdown_timeout_seconds)
                    else:
                        await asyncio.wait_for(
                            asyncio.to_thread(hook),
                            timeout=shutdown_timeout_seconds,
                        )
                except Exception:
                    LOGGER.exception("shutdown hook failed")

    # 组装应用骨架：FastAPI 实例、认证依赖、统一错误处理与请求观测中间件。
    app = FastAPI(title="FinanceClaw API", version="1.0.0", lifespan=lifespan)
    principal_dep = principal_dependency(authenticator)
    install_error_handlers(app)
    install_request_observability(app, p95_target_ms=p95_target_ms)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """存活探针（GET /health）：进程存活即返回 ok，不做依赖检查。"""
        return {"status": "ok", "stage": "5"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """就绪探针（GET /ready）：并发执行各依赖探针并汇总结果。

        生产装配下组合检查业务 PostgreSQL、Artifact Store 与 Agent
        Server；任一探针失败即整体未就绪。

        Returns:
            JSON 响应：全部通过时 200 与 ``status=ready``，否则 503 与
            ``status=unavailable``；``checks`` 携带各探针的布尔结果。

        """
        # 1. 取探针集合：未注入自定义探针时，默认只探 Agent Server 健康。
        checks = readiness_checks or {"agent_server": run_service.client.health}

        async def run_check(name: str, check: Callable[[], Awaitable[bool]]) -> tuple[str, bool]:
            """执行单个探针并限时，任何异常或超时都判为未就绪。"""
            try:
                result = await asyncio.wait_for(check(), timeout=readiness_timeout_seconds)
            except Exception:
                result = False
            return name, result

        # 2. 并发执行全部探针并逐项限时，避免单个依赖拖垮就绪判定。
        results = dict(
            await asyncio.gather(*(run_check(name, check) for name, check in checks.items()))
        )
        # 3. 全部通过才判就绪：200/ready，否则 503/unavailable。
        available = all(results.values())
        return JSONResponse(
            status_code=200 if available else 503,
            content={"status": "ready" if available else "unavailable", "checks": results},
        )

    @app.post(
        "/v1/runs",
        response_model=RunAccepted,
        status_code=202,
        include_in_schema=False,
    )
    async def start_run(
        request: RunRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ],
    ) -> RunAccepted:
        """直连运行入口（POST /v1/runs）：不经会话直接受理一次 Run。

        本路由从 OpenAPI 隐藏（``include_in_schema=False``），仅允许
        持有 ``internal:invoke`` scope 的服务身份调用；按目标类型分发：
        Workflow 目标走 WorkflowService（禁止挂在会话内），携带
        conversation_id 的转为会话轮次，否则走通用 RunService。

        Args:
            request: 运行请求体（消息正文与可选目标）。
            principal: 已认证的调用方身份。
            idempotency_key: 幂等键请求头（长度 1~200）。

        Returns:
            202 与运行受理回执（含 run_id 与初始状态）。

        Raises:
            WorkflowInputError: Workflow 目标同时携带会话 ID。
            RuntimeError: 目标类型对应的可选服务未装配。

        """
        # 1. 鉴权：直连入口只对内部服务身份开放。
        _require_internal_invocation(principal)
        # 2. 委派用例：按目标类型路由到 Workflow、会话轮次或通用 Run 通道。
        if isinstance(request.target, WorkflowTarget):
            if request.conversation_id is not None:
                raise WorkflowInputError(
                    "published workflows cannot run inside an Agent conversation"
                )
            if workflow_service is None:
                raise RuntimeError("workflow service is not configured")
            return await workflow_service.start(
                request.target,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
                scopes=principal.scopes,
                idempotency_key=idempotency_key,
            )
        if request.conversation_id is not None:
            if conversation_service is None:
                raise RuntimeError("conversation service is not configured")
            return await conversation_service.start_turn(
                request.conversation_id,
                ConversationTurnRequest(message=request.message),
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
                scopes=principal.scopes,
                idempotency_key=idempotency_key,
            )
        return await run_service.start(
            request,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
            scopes=principal.scopes,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/workflows/{workflow_id}/runs",
        response_model=RunAccepted,
        status_code=202,
        include_in_schema=False,
    )
    async def start_workflow(
        workflow_id: str,
        request: WorkflowInvokeRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ],
    ) -> RunAccepted:
        """直连 Workflow 入口（POST /v1/workflows/{workflow_id}/runs）。

        本路由从 OpenAPI 隐藏，仅允许 ``internal:invoke`` 服务身份；
        把路径中的 workflow_id 与请求体的版本、入参组装为
        ``WorkflowTarget`` 后交由 WorkflowService 受理。

        Args:
            workflow_id: 目标流程定义 ID（路径参数）。
            request: 调用请求体（版本与入参）。
            principal: 已认证的调用方身份。
            idempotency_key: 幂等键请求头（长度 1~200）。

        Returns:
            202 与运行受理回执（含 run_id 与初始状态）。

        Raises:
            RuntimeError: Workflow 服务未装配。

        """
        # 1. 鉴权：直连入口只对内部服务身份开放。
        _require_internal_invocation(principal)
        # 2. 委派用例：组装目标并交由 WorkflowService 受理。
        if workflow_service is None:
            raise RuntimeError("workflow service is not configured")
        return await workflow_service.start(
            WorkflowTarget(
                workflow_id=workflow_id,
                version=request.version,
                arguments=request.arguments,
            ),
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
            scopes=principal.scopes,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/tools/{tool_id}:invoke",
        response_model=RunAccepted,
        status_code=202,
        include_in_schema=False,
    )
    async def invoke_tool(
        tool_id: str,
        request: ToolInvokeRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ],
    ) -> RunAccepted:
        """直连工具入口（POST /v1/tools/{tool_id}:invoke）。

        本路由从 OpenAPI 隐藏，仅允许 ``internal:invoke`` 服务身份；
        把工具目标（版本与入参）包装为 ``RunRequest`` 后交由
        RunService 受理，与普通运行共用执行与治理路径。

        Args:
            tool_id: 目标工具 ID（路径参数）。
            request: 调用请求体（版本与入参）。
            principal: 已认证的调用方身份。
            idempotency_key: 幂等键请求头（长度 1~200）。

        Returns:
            202 与运行受理回执（含 run_id 与初始状态）。

        """
        # 1. 鉴权：直连入口只对内部服务身份开放。
        _require_internal_invocation(principal)
        # 2. 委派用例：把工具目标包装为 RunRequest 后交由 RunService 受理。
        run_request = RunRequest(
            message=f"Direct invocation of {tool_id}",
            target=ToolTarget(
                tool_id=tool_id, version=request.version, arguments=request.arguments
            ),
        )
        return await run_service.start(
            run_request,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
            scopes=principal.scopes,
            idempotency_key=idempotency_key,
        )

    @app.get("/v1/runs/{run_id}", response_model=RunStatusResponse)
    async def run_status(
        run_id: str,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
    ) -> RunStatusResponse:
        """运行状态查询（GET /v1/runs/{run_id}）。

        按通用 Run → Workflow → 会话 → 委派子运行的顺序回退查询，
        客户端无须区分运行通道即可获得统一的状态视图。

        Args:
            run_id: 运行 ID（路径参数）。
            principal: 已认证的调用方身份，用于租户隔离。

        Returns:
            运行当前状态与输出；到达终态后携带 output。

        Raises:
            RunNotFound: 全部通道都查不到该 ID（经错误映射返回 404）。

        """
        try:
            # 1. 先查通用 Run 通道。
            return await run_service.status(
                run_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )
        except RunNotFound:
            # 2. 回退查 Workflow 通道；仍查不到则继续。
            if workflow_service is not None:
                try:
                    return await workflow_service.status(
                        run_id,
                        tenant_id=principal.tenant_id,
                        subject_id=principal.subject_id,
                    )
                except RunNotFound:
                    pass
            # 3. 再回退查会话轮次通道。
            if conversation_service is not None:
                try:
                    return await conversation_service.status(
                        run_id,
                        tenant_id=principal.tenant_id,
                        subject_id=principal.subject_id,
                        scopes=principal.scopes,
                    )
                except RunNotFound:
                    pass
            # 4. 最后查委派子运行；查不到时由原异常兜底（映射 404）。
            if delegation_service is not None:
                return await delegation_service.child_status(
                    run_id,
                    tenant_id=principal.tenant_id,
                    subject_id=principal.subject_id,
                )
            raise

    @app.post("/v1/runs/{run_id}/resume", response_model=RunStatusResponse)
    async def resume_run(
        run_id: str,
        decision: ApprovalDecision,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
    ) -> RunStatusResponse:
        """审批恢复（POST /v1/runs/{run_id}/resume）。

        对因人机协同审批而挂起的运行提交决策（批准/驳回/修订入参），
        按通用 Run → Workflow → 会话的顺序回退定位被挂起的运行。

        Args:
            run_id: 挂起运行 ID（路径参数）。
            decision: 审批决策内容（类型、入参哈希与理由）。
            principal: 已认证的调用方身份，用于租户隔离。

        Returns:
            恢复处理后运行的最新状态。

        Raises:
            RunNotFound: 全部通道都查不到该运行（经错误映射返回 404）。

        """
        try:
            # 1. 先在通用 Run 通道恢复。
            return await run_service.resume(
                run_id,
                decision,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )
        except RunNotFound:
            # 2. 回退到 Workflow 通道恢复；仍查不到则继续。
            if workflow_service is not None:
                try:
                    return await workflow_service.resume(
                        run_id,
                        decision,
                        tenant_id=principal.tenant_id,
                        subject_id=principal.subject_id,
                        scopes=principal.scopes,
                    )
                except RunNotFound:
                    pass
            # 3. 再回退到会话通道恢复；查不到时由原异常兜底（映射 404）。
            if conversation_service is not None:
                return await conversation_service.resume(
                    run_id,
                    decision,
                    tenant_id=principal.tenant_id,
                    subject_id=principal.subject_id,
                    scopes=principal.scopes,
                )
            raise

    @app.get("/v1/runs/{run_id}/events")
    async def stream_run(
        run_id: str,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
    ) -> StreamingResponse:
        """运行事件流端点（GET /v1/runs/{run_id}/events，SSE）。

        先做归属校验（租户 + 主体），再按通用 Run → Workflow → 会话的
        顺序选定事件源，以 ``text/event-stream`` 持续下发 SSE 帧。

        Args:
            run_id: 运行 ID（路径参数）。
            principal: 已认证的调用方身份，用于归属校验。

        Returns:
            SSE 流式响应，事件帧由 ``project_sse`` 序列化。

        Raises:
            RunNotFound: 全部通道都查不到该运行（经错误映射返回 404）。

        """
        try:
            # 1. 归属校验并接入通用 Run 通道的事件流。
            run_service.assert_owned(
                run_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )
            events = run_service.stream(
                run_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )
        except RunNotFound:
            # 2. 回退 Workflow 通道：先校验归属，再接入其事件流。
            found_workflow = False
            if workflow_service is not None:
                try:
                    await workflow_service.assert_owned(
                        run_id,
                        tenant_id=principal.tenant_id,
                        subject_id=principal.subject_id,
                    )
                    events = workflow_service.stream(
                        run_id,
                        tenant_id=principal.tenant_id,
                        subject_id=principal.subject_id,
                    )
                    found_workflow = True
                except RunNotFound:
                    pass
            # 3. 再回退会话通道：同步的归属校验放到线程池，避免阻塞事件循环。
            if not found_workflow:
                if conversation_service is None:
                    raise
                await asyncio.to_thread(
                    conversation_service.assert_owned,
                    run_id,
                    tenant_id=principal.tenant_id,
                    subject_id=principal.subject_id,
                )
                events = conversation_service.stream(
                    run_id,
                    tenant_id=principal.tenant_id,
                    subject_id=principal.subject_id,
                )
        # 4. 统一以 SSE 响应输出所选事件源。
        return StreamingResponse(project_sse(events), media_type="text/event-stream")

    if conversation_service is not None:

        @app.post("/v1/conversations", response_model=ConversationResponse, status_code=201)
        async def create_conversation(
            _request: CreateConversationRequest,
            principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        ) -> ConversationResponse:
            """创建会话（POST /v1/conversations）：产品级写入口之一。

            请求体当前无必填字段，仅作契约占位；会话归属取自调用方
            身份。成功返回 201 与会话基础信息（ID、状态与创建时间）。

            Args:
                _request: 创建会话请求体（无字段，契约占位）。
                principal: 已认证的调用方身份，决定会话归属。

            Returns:
                201 与会话基础信息。

            """
            return await conversation_service.create(
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )

        @app.post(
            "/v1/conversations/{conversation_id}/turns",
            response_model=ConversationTurnAccepted,
            status_code=202,
        )
        async def start_conversation_turn(
            conversation_id: str,
            request: ConversationTurnRequest,
            principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
            ],
        ) -> ConversationTurnAccepted:
            """提交 message-only Turn（POST /v1/conversations/{id}/turns）。

            产品唯一的产品写入口：请求体只携带 message；``/tool <id>``、
            ``/workflow <id>``、``/agent <id>`` 斜杠指令写在 message 中
            表达调用偏好，由会话服务解析路由。支持 ``Idempotency-Key``
            幂等重放，重复提交返回相同回执。

            Args:
                conversation_id: 目标会话 ID（路径参数）。
                request: 轮次请求体（仅 message 字段）。
                principal: 已认证的调用方身份。
                idempotency_key: 幂等键请求头（长度 1~200）。

            Returns:
                202 与轮次受理回执（run_id、conversation_id、turn_id 等）。

            Raises:
                RuntimeError: 服务回执缺失 conversation_id/turn_id。

            """
            # 1. 委派用例：创建轮次并受理运行。
            accepted = await conversation_service.start_turn(
                conversation_id,
                request,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
                scopes=principal.scopes,
                idempotency_key=idempotency_key,
            )
            # 2. 回执完整性兜底：会话 ID 与轮次 ID 必须齐备才对外返回。
            if accepted.conversation_id is None or accepted.turn_id is None:
                raise RuntimeError("conversation turn acknowledgement is incomplete")
            return ConversationTurnAccepted(
                run_id=accepted.run_id,
                status=accepted.status,
                idempotent_replay=accepted.idempotent_replay,
                conversation_id=accepted.conversation_id,
                turn_id=accepted.turn_id,
            )

        @app.get("/v1/conversations/{conversation_id}", response_model=ConversationResponse)
        def get_conversation(
            conversation_id: str,
            principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        ) -> ConversationResponse:
            """查询会话基础信息（GET /v1/conversations/{conversation_id}）。

            Args:
                conversation_id: 会话 ID（路径参数）。
                principal: 已认证的调用方身份，用于租户隔离。

            Returns:
                会话基础信息（ID、状态与创建时间）。

            """
            return conversation_service.get(
                conversation_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )

        @app.get(
            "/v1/conversations/{conversation_id}/messages",
            response_model=ConversationMessagesResponse,
        )
        def get_conversation_messages(
            conversation_id: str,
            principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        ) -> ConversationMessagesResponse:
            """查询会话历史消息（GET /v1/conversations/{id}/messages）。

            Args:
                conversation_id: 会话 ID（路径参数）。
                principal: 已认证的调用方身份，用于租户隔离。

            Returns:
                会话全部消息，按会话语义顺序返回。

            """
            return conversation_service.messages(
                conversation_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )

    return app


def create_default_app(settings: FinanceClawSettings | None = None) -> FastAPI:
    """按配置全量装配生产可用的 BFF 应用（含持久化与可观测性）。

    使用场景：服务器启动入口（如 uvicorn 工厂）调用；依据
    ``FinanceClawSettings`` 构建各应用服务、认证器、就绪探针与关闭
    钩子，并完成 JSON 日志、LangSmith 与 OTel 的初始化。

    Args:
        settings: 可选配置；为 None 时从环境变量加载默认配置。

    Returns:
        装配完成的 FastAPI 应用，数据库句柄挂在 ``app.state`` 上。

    Raises:
        RuntimeError: 会话/Workflow/委派的持久化组件未配置。

    """
    # 1. 解析配置并初始化可观测性：JSON 日志、LangSmith 追踪与 OTel。
    settings = settings or FinanceClawSettings()
    configure_json_logging(settings.log_level)
    configure_langsmith(
        project=settings.langsmith_project,
        endpoint=settings.langsmith_endpoint,
        sample_rate=settings.langsmith_trace_sample_rate,
        hide_inputs=settings.langsmith_hide_inputs,
        hide_outputs=settings.langsmith_hide_outputs,
    )
    telemetry: TelemetryRuntime = configure_telemetry(
        service_name=settings.otel_service_name,
        environment=settings.environment.value,
        endpoint=settings.otel_exporter_endpoint,
        metrics_endpoint=settings.otel_metrics_exporter_endpoint,
        sample_rate=settings.otel_trace_sample_rate,
    )
    # 2. 装配基础设施组件：工具目录、Agent 档案、仓库、审计与制品服务。
    components = build_components(settings, enable_persistence=True)
    service_token = (
        settings.agent_server_service_token.get_secret_value()
        if settings.agent_server_service_token is not None
        else None
    )
    # 3. 构建 Agent Server 客户端与目标解析器，作为各服务的执行底座。
    client = LangGraphAgentServerClient(
        url=settings.agent_server_url,
        service_token=service_token,
        timeout_seconds=settings.agent_server_timeout_seconds,
    )
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
        workflow_catalog=components.workflow_catalog,
    )
    # 4. 装配应用服务：Run、Workflow、委派与会话（会话依赖委派服务）。
    run_service = RunService(client, resolver)
    if components.conversation_repository is None:
        raise RuntimeError("conversation persistence was not configured")
    if components.workflow_repository is None or components.workflow_catalog is None:
        raise RuntimeError("workflow persistence was not configured")
    workflow_service = WorkflowService(
        client,
        components.workflow_repository,
        components.workflow_catalog,
        components.audit,
    )
    if components.delegation_repository is None:
        raise RuntimeError("delegation persistence was not configured")
    delegation_service = DelegationService(
        client,
        components.delegation_repository,
        workflow_service,
        components.agent_profiles,
        components.audit,
    )
    conversation_service = ConversationService(
        client,
        components.conversation_repository,
        components.agent_profiles,
        delegation_service=delegation_service,
        summary_service=components.summary_service,
        approval_timeout_seconds=settings.approval_timeout_seconds,
    )
    # 5. 选择认证器：OIDC 配置齐备时用 JWT 校验，否则退化为静态 token
    #    （仅限本地开发，生产配置校验会禁止后者）。
    if settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
        authenticator: Authenticator = OIDCJWTAuthenticator(
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
    else:
        principals = {}
        if settings.bff_auth_token is not None:
            principals[settings.bff_auth_token.get_secret_value()] = AuthenticatedPrincipal(
                tenant_id=settings.bff_tenant_id,
                subject_id=settings.bff_subject_id,
                scopes=settings.bff_scopes,
            )
        authenticator = StaticBearerAuthenticator(principals)

    async def database_ready() -> bool:
        """探测业务 PostgreSQL 连通性；数据库未配置时判为未就绪。"""
        if components.database is None:
            return False
        return await asyncio.to_thread(components.database.ping)

    async def artifact_ready() -> bool:
        """探测 Artifact Store 健康度；制品服务未配置时判为未就绪。"""
        if components.artifact_service is None:
            return False
        return await asyncio.to_thread(components.artifact_service.store.health)

    # 6. 汇总就绪探针与关闭钩子：lifespan 逆序执行，先关数据库、最后 flush OTel。
    shutdown_hooks: list[Callable[[], Any]] = [telemetry.shutdown]
    if components.database is not None:
        shutdown_hooks.append(components.database.close)
    # 7. 装配 FastAPI 应用，并把数据库句柄挂到 app.state 供运维复用。
    app = create_app(
        run_service=run_service,
        authenticator=authenticator,
        conversation_service=conversation_service,
        workflow_service=workflow_service,
        delegation_service=delegation_service,
        readiness_checks={
            "database": database_ready,
            "artifact_store": artifact_ready,
            "agent_server": client.health,
        },
        shutdown_hooks=tuple(shutdown_hooks),
        readiness_timeout_seconds=settings.readiness_timeout_seconds,
        shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
        p95_target_ms=settings.api_p95_target_ms,
    )
    app.state.financeclaw_database = components.database
    return app
