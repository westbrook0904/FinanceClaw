"""Conversation-first product BFF with internal control-plane compatibility."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from financeclaw.application import (
    ConversationService,
    LangGraphAgentServerClient,
    RunNotFound,
    RunService,
    TargetResolver,
    WorkflowInputError,
    WorkflowService,
)
from financeclaw.bootstrap import build_components
from financeclaw.contracts import (
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
from financeclaw.infrastructure import FinanceClawSettings

from .auth import (
    AuthenticatedPrincipal,
    Authenticator,
    StaticBearerAuthenticator,
    principal_dependency,
)
from .errors import install_error_handlers
from .streaming import project_sse

LOGGER = logging.getLogger(__name__)
_INTERNAL_INVOKE_SCOPE = "internal:invoke"


def _require_internal_invocation(principal: AuthenticatedPrincipal) -> None:
    """Keep deterministic graph entry points out of the product trust boundary."""

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
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if conversation_service is not None:
            try:
                await conversation_service.reconcile_incomplete()
            except Exception:
                LOGGER.exception("conversation reconciliation deferred after startup failure")
        if workflow_service is not None:
            try:
                await workflow_service.reconcile_incomplete()
            except Exception:
                LOGGER.exception("workflow reconciliation deferred after startup failure")
        yield

    app = FastAPI(title="FinanceClaw API", version="1.0.0", lifespan=lifespan)
    principal_dep = principal_dependency(authenticator)
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "stage": "4"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        available = await run_service.client.health()
        return JSONResponse(
            status_code=200 if available else 503,
            content={"status": "ready" if available else "unavailable", "agent_server": available},
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
                return await conversation_service.status(
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
            return conversation_service.messages(
                conversation_id,
                tenant_id=principal.tenant_id,
                subject_id=principal.subject_id,
            )

    return app


def create_default_app(settings: FinanceClawSettings | None = None) -> FastAPI:
    settings = settings or FinanceClawSettings()
    components = build_components(settings, enable_persistence=True)
    service_token = (
        settings.agent_server_service_token.get_secret_value()
        if settings.agent_server_service_token is not None
        else None
    )
    client = LangGraphAgentServerClient(url=settings.agent_server_url, service_token=service_token)
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
        workflow_catalog=components.workflow_catalog,
    )
    run_service = RunService(client, resolver)
    if components.conversation_repository is None:
        raise RuntimeError("conversation persistence was not configured")
    conversation_service = ConversationService(
        client,
        components.conversation_repository,
        components.agent_profiles,
        summary_service=components.summary_service,
        approval_timeout_seconds=settings.approval_timeout_seconds,
    )
    if components.workflow_repository is None or components.workflow_catalog is None:
        raise RuntimeError("workflow persistence was not configured")
    workflow_service = WorkflowService(
        client,
        components.workflow_repository,
        components.workflow_catalog,
        components.audit,
    )
    principals = {}
    if settings.bff_auth_token is not None:
        principals[settings.bff_auth_token.get_secret_value()] = AuthenticatedPrincipal(
            tenant_id=settings.bff_tenant_id,
            subject_id=settings.bff_subject_id,
            scopes=settings.bff_scopes,
        )
    app = create_app(
        run_service=run_service,
        authenticator=StaticBearerAuthenticator(principals),
        conversation_service=conversation_service,
        workflow_service=workflow_service,
    )
    app.state.financeclaw_database = components.database
    return app
