"""Framework-independent HTTP composition; Agent Server owns the outer ASGI application."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from financeclaw.api.http.errors import install_error_handlers
from financeclaw.api.http.routers import product_router
from financeclaw.shared.infrastructure.observability.telemetry import install_request_observability


def create_app(
    *,
    turns,
    conversations,
    authenticator,
    startup_hooks=(),
    shutdown_hooks=(),
    readiness_checks=None,
    p95_target_ms=500,
):
    """Compose product HTTP routes with injected services for isolated integration tests."""

    @asynccontextmanager
    async def lifespan(app):
        """Start only this process role and drain its responsibilities before closing resources."""
        try:
            for hook in startup_hooks:
                await hook()
            yield
        finally:
            for hook in reversed(shutdown_hooks):
                await hook()

    app = FastAPI(title="FinanceClaw API", version="10", lifespan=lifespan)
    install_error_handlers(app)
    install_request_observability(app, p95_target_ms=p95_target_ms)
    app.include_router(product_router(conversations, turns, authenticator))

    @app.get("/v1/health/live")
    async def live():
        """Report that the product ASGI process is responding."""
        return {"status": "ok"}

    @app.get("/v1/health/ready")
    async def ready():
        """Check application persistence and required background responsibilities."""
        checks = readiness_checks or {}

        async def check(fn):
            """Bound readiness latency and treat unavailable dependencies as unhealthy."""
            try:
                return bool(await asyncio.wait_for(fn(), timeout=3))
            except Exception:
                return False

        values = dict(
            zip(checks, await asyncio.gather(*(check(fn) for fn in checks.values())), strict=True)
        )
        return JSONResponse(
            status_code=200 if all(values.values()) else 503, content={"checks": values}
        )

    return app
