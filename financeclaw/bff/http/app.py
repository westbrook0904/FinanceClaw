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

from financeclaw.bff.application.conversation_service import ConversationService
from financeclaw.bff.http.auth import (
    AuthenticatedPrincipal,
    Authenticator,
    principal_dependency,
)
from financeclaw.bff.http.errors import install_error_handlers
from financeclaw.bff.http.streaming import project_sse
from financeclaw.coordination.api import (
    DelegationService,
    RunNotFound,
    RunService,
    WorkflowInputError,
    WorkflowService,
)
from financeclaw.kernel.interactions import InteractionResponse
from financeclaw.kernel.responses import (
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
    WorkflowInvokeRequest,
)
from financeclaw.kernel.targets import ToolTarget, WorkflowTarget
from financeclaw.shared.infrastructure.observability.telemetry import (
    install_request_observability,
)

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
    startup_hooks: tuple[Callable[[], Any], ...] = (),
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
        startup_hooks: 启动期回调元组；异常会使应用启动失败。
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
        # 2. 启动协议适配器等外部生命周期组件；失败时让应用 fail fast。
        try:
            for hook in startup_hooks:
                if inspect.iscoroutinefunction(hook):
                    await hook()
                else:
                    result = await asyncio.to_thread(hook)
                    if inspect.isawaitable(result):
                        await result
            # 3. 让出控制权对外服务，直到进程关闭再进入 finally 收尾。
            yield
        finally:
            # 4. 关闭收尾：按注册逆序执行钩子（如先关 Channel/数据库，再 flush OTel）；
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

    interaction_service = (
        conversation_service.runs.interactions
        if conversation_service
        else delegation_service.interactions
        if delegation_service
        else workflow_service.interactions
        if workflow_service
        else None
    )
    if interaction_service is not None and workflow_service is not None:
        interaction_service.workflow_catalog = workflow_service.catalog

    @app.get("/v1/interactions/{interaction_id}")
    async def interaction_status(
        interaction_id: str, principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)]
    ) -> dict[str, Any]:
        """按 ID 查询交互安全投影，旧入口也可定位；不恢复任何执行。"""
        if interaction_service is None:
            raise HTTPException(status_code=503, detail="interaction service is unavailable")
        row = await asyncio.to_thread(
            interaction_service.repository.get_owned,
            interaction_id,
            principal.tenant_id,
            principal.subject_id,
            now=interaction_service.clock(),
        )
        return await interaction_service.public(row)

    @app.post("/v1/interactions/{interaction_id}/responses", status_code=202)
    async def interaction_response(
        interaction_id: str,
        request: InteractionResponse,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
    ) -> dict[str, Any]:
        """资料、选择、审批分型提交；202 表示决定受理，不等于底层完成。"""
        if interaction_service is None:
            raise HTTPException(status_code=503, detail="interaction service is unavailable")
        return await interaction_service.respond(
            interaction_id,
            request,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
            scopes=principal.scopes,
            idempotency_key=idempotency_key,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """存活探针（GET /health）：进程存活即返回 ok，不做依赖检查。"""
        return {"status": "ok", "stage": "6"}

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
                        scopes=principal.scopes,
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
                    scopes=principal.scopes,
                )
            raise

    @app.post("/v1/runs/{run_id}/cancel", response_model=RunStatusResponse)
    async def cancel_run(
        run_id: str,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
    ) -> RunStatusResponse:
        """取消拥有的根会话或独立 Workflow；子任务须通过根运行统一停止。"""
        if conversation_service is not None:
            try:
                return await conversation_service.cancel(
                    run_id, tenant_id=principal.tenant_id, subject_id=principal.subject_id
                )
            except RunNotFound:
                pass
        if workflow_service is not None:
            return await workflow_service.cancel(
                run_id, tenant_id=principal.tenant_id, subject_id=principal.subject_id
            )
        raise RunNotFound("root task cancellation is not configured")

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
                    scopes=principal.scopes,
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
