"""BFF 装配入口：认证、HTTP、Channel 和嵌入式协调服务。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI

from financeclaw.bff.application.conversation_service import ConversationService
from financeclaw.bff.application.feishu_channel_service import FeishuChannelService
from financeclaw.bff.channels.feishu import FeishuChannelAdapter
from financeclaw.bff.http.app import create_app
from financeclaw.bff.http.auth import (
    AuthenticatedPrincipal,
    Authenticator,
    OIDCJWTAuthenticator,
    StaticBearerAuthenticator,
)
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.shared.infrastructure.observability.langsmith import configure_langsmith
from financeclaw.shared.infrastructure.observability.logging import configure_json_logging
from financeclaw.shared.infrastructure.observability.telemetry import (
    TelemetryRuntime,
    configure_telemetry,
)
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


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
    coordination = build_coordination(settings)
    components = coordination.resources
    client = coordination.client
    run_service = coordination.runs
    workflow_service = coordination.workflows
    delegation_service = coordination.delegations
    conversation_service = ConversationService(
        components.conversation_repository,
        coordination.releases.agent_profiles,
        runs=coordination.conversations,
    )
    # 5. 飞书 Channel 默认关闭；开启时才构造 SDK 适配器并在 lifespan 中连接。
    feishu_channel: FeishuChannelAdapter | None = None
    if settings.feishu_enabled:
        if settings.feishu_app_id is None or settings.feishu_app_secret is None:
            raise RuntimeError("Feishu channel configuration is incomplete")
        feishu_service = FeishuChannelService(
            conversation_service,
            app_id=settings.feishu_app_id,
            allowed_open_ids=settings.feishu_allowed_open_ids,
            scopes=settings.feishu_scopes,
            max_concurrency=settings.feishu_max_concurrency,
        )
        feishu_channel = FeishuChannelAdapter(
            feishu_service,
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret.get_secret_value(),
            allowed_open_ids=settings.feishu_allowed_open_ids,
            max_concurrency=settings.feishu_max_concurrency,
            security_mode=settings.feishu_security_mode,
            connect_timeout_seconds=settings.feishu_connect_timeout_seconds,
        )
    # 6. 选择认证器：OIDC 配置齐备时用 JWT 校验，否则退化为静态 token
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

    # 7. 汇总就绪探针与关闭钩子：逆序执行，先关 Channel/数据库、最后 flush OTel。
    shutdown_hooks: list[Callable[[], Any]] = [telemetry.shutdown]
    if components.database is not None:
        shutdown_hooks.append(components.database.close)
    startup_hooks: tuple[Callable[[], Any], ...] = ()
    readiness_checks: dict[str, Callable[[], Awaitable[bool]]] = {
        "database": database_ready,
        "artifact_store": artifact_ready,
        "agent_server": client.health,
    }
    if feishu_channel is not None:
        startup_hooks = (feishu_channel.start,)
        shutdown_hooks.append(feishu_channel.stop)
        readiness_checks["feishu_channel"] = feishu_channel.health
    # 8. 装配 FastAPI 应用，并把数据库与 Channel 句柄挂到 app.state 供运维复用。
    app = create_app(
        run_service=run_service,
        authenticator=authenticator,
        conversation_service=conversation_service,
        workflow_service=workflow_service,
        delegation_service=delegation_service,
        readiness_checks=readiness_checks,
        startup_hooks=startup_hooks,
        shutdown_hooks=tuple(shutdown_hooks),
        readiness_timeout_seconds=settings.readiness_timeout_seconds,
        shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
        p95_target_ms=settings.api_p95_target_ms,
    )
    app.state.financeclaw_database = components.database
    app.state.financeclaw_feishu_channel = feishu_channel
    return app
