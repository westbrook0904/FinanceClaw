"""创建 FastAPI 路由，并将鉴权后的 HTTP 请求交给应用服务。"""

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

LOGGER = logging.getLogger(__name__)
_INTERNAL_INVOKE_SCOPE = "internal:invoke"


def _require_internal_invocation(principal: AuthenticatedPrincipal) -> None:
    """要求身份含内部调用权限；缺少权限时立即拒绝管理型接口。"""
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
    """创建并返回新的app 模块的数据。"""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """在 FastAPI 启停边界执行启动准备与限时关闭钩子。"""
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
        try:
            yield
        finally:
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

    app = FastAPI(title="FinanceClaw API", version="1.0.0", lifespan=lifespan)
    principal_dep = principal_dependency(authenticator)
    install_error_handlers(app)
    install_request_observability(app, p95_target_ms=p95_target_ms)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
        return {"status": "ok", "stage": "5"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """并发运行全部就绪检查，仅在每项依赖均可用时返回成功。"""
        checks = readiness_checks or {"agent_server": run_service.client.health}

        async def run_check(name: str, check: Callable[[], Awaitable[bool]]) -> tuple[str, bool]:
            """为单个就绪检查施加超时，并把异常归一化为不可用。"""
            try:
                result = await asyncio.wait_for(check(), timeout=readiness_timeout_seconds)
            except Exception:
                result = False
            return name, result

        results = dict(
            await asyncio.gather(*(run_check(name, check) for name, check in checks.items()))
        )
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
        """校验输入后启动app 模块的数据，返回可供后续查询的记录。"""
        _require_internal_invocation(principal)
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
        """校验输入后启动app 模块的数据，返回可供后续查询的记录。"""
        _require_internal_invocation(principal)
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
        """将直接工具 HTTP 请求转换为统一运行请求并交给运行服务。"""
        _require_internal_invocation(principal)
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
        """根据运行所属服务查询状态，并在找不到时尝试其他运行类型。"""
        try:
            return await run_service.status(
                run_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )
        except RunNotFound:
            if workflow_service is not None:
                try:
                    return await workflow_service.status(
                        run_id,
                        tenant_id=principal.tenant_id,
                        subject_id=principal.subject_id,
                    )
                except RunNotFound:
                    pass
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
        """使用审批决定恢复中断的app 模块的数据。"""
        try:
            return await run_service.resume(
                run_id,
                decision,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )
        except RunNotFound:
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
        """校验访问权限后流式输出app 模块的数据事件。"""
        try:
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
        return StreamingResponse(project_sse(events), media_type="text/event-stream")

    if conversation_service is not None:

        @app.post("/v1/conversations", response_model=ConversationResponse, status_code=201)
        async def create_conversation(
            _request: CreateConversationRequest,
            principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        ) -> ConversationResponse:
            """创建并返回新的app 模块的数据。"""
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
            """校验输入后启动app 模块的数据，返回可供后续查询的记录。"""
            accepted = await conversation_service.start_turn(
                conversation_id,
                request,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
                scopes=principal.scopes,
                idempotency_key=idempotency_key,
            )
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
            """按标识读取app 模块的数据；不存在时由下层仓储抛出明确异常。"""
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
            """按标识读取app 模块的数据；不存在时由下层仓储抛出明确异常。"""
            return conversation_service.messages(
                conversation_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )

    return app


def create_default_app(settings: FinanceClawSettings | None = None) -> FastAPI:
    """创建并返回新的app 模块的数据。"""
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
    components = build_components(settings, enable_persistence=True)
    service_token = (
        settings.agent_server_service_token.get_secret_value()
        if settings.agent_server_service_token is not None
        else None
    )
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
        """通过轻量查询检查数据库是否可接受请求。"""
        if components.database is None:
            return False
        return await asyncio.to_thread(components.database.ping)

    async def artifact_ready() -> bool:
        """验证制品存储可用；当前实现确认服务已成功组装。"""
        if components.artifact_service is None:
            return False
        return await asyncio.to_thread(components.artifact_service.store.health)

    shutdown_hooks: list[Callable[[], Any]] = [telemetry.shutdown]
    if components.database is not None:
        shutdown_hooks.append(components.database.close)
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
