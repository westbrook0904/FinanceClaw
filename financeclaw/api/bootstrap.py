"""Custom Agent Server app, with explicit role ownership of background services."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


def create_default_app(settings=None, *, resources=None, client=None):
    """Build the custom AgentServer app, registering routes before native startup."""
    shared_process = settings is None
    settings = settings or FinanceClawSettings()

    @asynccontextmanager
    async def lifespan(app):
        """Start only this process role and drain its responsibilities before closing resources."""
        from financeclaw.shared.infrastructure.resources import build_resources
        from financeclaw.shared.infrastructure.runtime import (
            close_process_resources,
            process_resources,
        )

        shared = resources or (
            process_resources()
            if shared_process
            else build_resources(settings, enable_persistence=True)
        )
        from financeclaw.shared.turns.repository import TurnRepository

        TurnRepository(shared.database.session_factory).require_schema()
        turns = None
        try:
            if settings.process_role == "api":
                from financeclaw.api.application.conversation_service import ConversationService
                from financeclaw.api.application.turns.bootstrap import build_turns

                turns = build_turns(settings, shared, client=client)
                conversations = ConversationService(
                    shared.conversation_repository, turns.releases.agents, turns=turns
                )
                app.state.conversations = conversations
                if settings.feishu_enabled:
                    from financeclaw.api.application.feishu_channel_service import (
                        FeishuChannelService,
                    )

                    app.state.feishu = FeishuChannelService(
                        conversations,
                        app_id=settings.feishu_app_id,
                        allowed_open_ids=settings.feishu_allowed_open_ids,
                        scopes=settings.feishu_scopes,
                        max_concurrency=settings.feishu_max_concurrency,
                    )
                app.state.turns = turns
                await turns.events.start()
                await turns.lifecycle.start()
            app.state.resources = shared
            yield
        finally:
            if turns:
                await turns.lifecycle.stop()
                await turns.events.stop()
                if client is None:
                    await turns.lifecycle.native.client.aclose()
            if resources is None:
                await run_sync(close_process_resources if shared_process else shared.database.close)

    app = FastAPI(title="FinanceClaw API", version="10", lifespan=lifespan)
    from financeclaw.api.http.errors import install_error_handlers
    from financeclaw.shared.infrastructure.observability.telemetry import (
        install_request_observability,
    )

    install_error_handlers(app)
    if settings.process_role == "api":
        from financeclaw.api.authentication import build_authenticator
        from financeclaw.api.http.routers import product_router

        class ServiceProxy:
            """Resolve lifespan-owned services after native route registration."""

            def __init__(self, name):
                """Inject dependencies without starting background work."""
                self.name = name

            def __getattr__(self, key):
                """Resolve the service initialized by the application lifespan."""
                return getattr(getattr(app.state, self.name), key)

        app.include_router(
            product_router(
                ServiceProxy("conversations"), ServiceProxy("turns"), build_authenticator(settings)
            )
        )
        if settings.feishu_enabled:
            from financeclaw.api.http.channels import channel_router

            app.include_router(channel_router(ServiceProxy("feishu"), settings))
    install_request_observability(app, p95_target_ms=settings.api_p95_target_ms)

    @app.get("/v1/health/live")
    async def live():
        """Report that the product ASGI process is responding."""
        return {"status": "ok", "role": settings.process_role}

    @app.get("/v1/health/ready")
    async def ready():
        """Check application persistence and required background responsibilities."""
        from fastapi.responses import JSONResponse

        shared = getattr(app.state, "resources", None)
        turns = getattr(app.state, "turns", None)
        valid = False
        try:
            async with asyncio.timeout(4):
                valid = shared is not None and await run_sync(shared.database.ping)
                valid = valid and await run_sync(shared.artifact_service.store.health)
                if settings.process_role == "api":
                    valid = (
                        valid
                        and turns is not None
                        and await turns.lifecycle.healthy()
                        and await turns.events.healthy()
                        and await run_sync(turns.store.responsibility_healthy)
                    )
                    if client is None and valid:
                        await turns.lifecycle.native.client.threads.search(limit=1)
        except Exception:
            valid = False
        return JSONResponse(status_code=200 if valid else 503, content={"ready": bool(valid)})

    return app


app = create_default_app()
