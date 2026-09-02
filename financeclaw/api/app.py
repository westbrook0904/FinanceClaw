"""Minimal product BFF for run, stream, direct Tool, approval and readiness."""

from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse

from financeclaw.application import LangGraphAgentServerClient, RunService, TargetResolver
from financeclaw.bootstrap import build_components
from financeclaw.contracts import (
    ApprovalDecision,
    RunAccepted,
    RunRequest,
    RunStatusResponse,
    ToolInvokeRequest,
    ToolTarget,
)
from financeclaw.infrastructure import FinanceClawSettings

from .auth import (
    AuthenticatedPrincipal,
    Authenticator,
    StaticBearerAuthenticator,
    principal_dependency,
)
from .errors import install_error_handlers
from .streaming import project_sse


def create_app(*, run_service: RunService, authenticator: Authenticator) -> FastAPI:
    app = FastAPI(title="FinanceClaw API", version="1.0.0")
    principal_dep = principal_dependency(authenticator)
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "stage": "1"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        available = await run_service.client.health()
        return JSONResponse(
            status_code=200 if available else 503,
            content={"status": "ready" if available else "unavailable", "agent_server": available},
        )

    @app.post("/v1/runs", response_model=RunAccepted, status_code=202)
    async def start_run(
        request: RunRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ],
    ) -> RunAccepted:
        return await run_service.start(
            request,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
            scopes=principal.scopes,
            idempotency_key=idempotency_key,
        )

    @app.post("/v1/tools/{tool_id}:invoke", response_model=RunAccepted, status_code=202)
    async def invoke_tool(
        tool_id: str,
        request: ToolInvokeRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ],
    ) -> RunAccepted:
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
        return await run_service.status(
            run_id,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
        )

    @app.post("/v1/runs/{run_id}/resume", response_model=RunStatusResponse)
    async def resume_run(
        run_id: str,
        decision: ApprovalDecision,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
    ) -> RunStatusResponse:
        return await run_service.resume(
            run_id,
            decision,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
        )

    @app.get("/v1/runs/{run_id}/events")
    async def stream_run(
        run_id: str,
        principal: Annotated[AuthenticatedPrincipal, Depends(principal_dep)],
    ) -> StreamingResponse:
        events = run_service.stream(
            run_id,
            tenant_id=principal.tenant_id,
            subject_id=principal.subject_id,
        )
        return StreamingResponse(project_sse(events), media_type="text/event-stream")

    return app


def create_default_app(settings: FinanceClawSettings | None = None) -> FastAPI:
    settings = settings or FinanceClawSettings()
    components = build_components(settings)
    service_token = (
        settings.agent_server_service_token.get_secret_value()
        if settings.agent_server_service_token is not None
        else None
    )
    client = LangGraphAgentServerClient(url=settings.agent_server_url, service_token=service_token)
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
    )
    run_service = RunService(client, resolver)
    principals = {}
    if settings.bff_auth_token is not None:
        principals[settings.bff_auth_token.get_secret_value()] = AuthenticatedPrincipal(
            tenant_id=settings.bff_tenant_id,
            subject_id=settings.bff_subject_id,
            scopes=settings.bff_scopes,
        )
    return create_app(
        run_service=run_service,
        authenticator=StaticBearerAuthenticator(principals),
    )
