"""受认证的内部 LangGraph Webhook 入口，不执行任何协调或远端任务。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from hmac import compare_digest

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from financeclaw.coordination.backends.langgraph_protocol import decode_notification
from financeclaw.coordination.repository import DRIVER_VERSION, CoordinatorRepository, now
from financeclaw.shared.execution_ledger.coordination_tables import CoordinatorHeartbeatRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


def create_ingress(repository, settings, *, close=None):
    """注入存储和部署身份；业务库不可用返回失败，backend 会按原生策略重试。"""
    if not settings.coordinator_enabled:
        raise ValueError("Coordinator Ingress is disabled")
    repository.require_schema()

    @asynccontextmanager
    async def lifespan(_app):
        """只管理入口资源，不隐式启动 Worker。"""
        try:
            yield
        finally:
            if close:
                await asyncio.to_thread(close)

    app = FastAPI(title="FinanceClaw Coordinator Ingress", lifespan=lifespan)

    @app.get("/health")
    def health():
        """入口进程存活。"""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        """数据库 schema 与兼容 Worker 心跳分别诊断。"""

        def check():
            """查询兼容 Worker 的近期心跳，不推进任务。"""
            repository.require_schema()
            with repository.sessions() as session:
                return (
                    session.scalar(
                        select(CoordinatorHeartbeatRow.worker_id)
                        .where(
                            CoordinatorHeartbeatRow.backend_instance_id
                            == repository.backend_instance_id,
                            CoordinatorHeartbeatRow.driver_version == DRIVER_VERSION,
                            CoordinatorHeartbeatRow.heartbeat_at
                            > now()
                            - timedelta(seconds=max(10, settings.coordinator_lease_seconds)),
                        )
                        .limit(1)
                    )
                    is not None
                )

        try:
            worker = await asyncio.to_thread(check)
        except Exception:
            return JSONResponse({"database": "unavailable", "worker": "unknown"}, status_code=503)
        return JSONResponse(
            {"database": "ready", "worker": "ready" if worker else "unavailable"},
            status_code=200 if worker else 503,
        )

    @app.post("/internal/webhooks/{backend_instance_id}", status_code=204)
    async def webhook(backend_instance_id: str, request: Request):
        """先认证再读取有界 body，仅保存最小字段；提交失败不会发送成功回执。"""
        expected = "Bearer " + settings.coordinator_webhook_token.get_secret_value()
        if backend_instance_id != repository.backend_instance_id or not compare_digest(
            request.headers.get("authorization", ""), expected
        ):
            raise HTTPException(401, "invalid backend authentication")
        if request.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(415, "content encoding is not supported")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "webhook exceeds body limit")
        try:
            notification = decode_notification(bytes(body), backend_instance_id=backend_instance_id)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "invalid backend notification") from None
        try:
            await asyncio.to_thread(repository.notify, notification)
        except Exception:
            raise HTTPException(503, "notification persistence unavailable") from None
        return Response(status_code=204)

    return app


def create_default_ingress():
    """Uvicorn financeclaw.coordination.ingress.app:create_default_ingress --factory。"""
    settings = FinanceClawSettings()
    database = ApplicationDatabase(
        settings.database_url.get_secret_value(),
        statement_timeout_seconds=settings.database_statement_timeout_seconds,
    )
    if settings.database_auto_create_schema:
        database.initialize_schema()
    repository = CoordinatorRepository(
        database.session_factory, backend_instance_id=settings.coordinator_backend_instance_id
    )
    return create_ingress(repository, settings, close=database.close)
