"""确定性优先、只在明确 no-match 时降级的路由流水线。"""

from __future__ import annotations

from harness_contracts import ErrorCode, RouteDecision, RoutingError

from .models import RoutingContext
from .router import Router


class RoutingPipeline(Router):
    """组合静态 Router 与模型补全 Router，并冻结唯一降级条件。"""

    def __init__(
        self,
        deterministic_router: Router,
        model_router: Router,
        *,
        router_id: str = "routing-pipeline",
    ) -> None:
        if not isinstance(deterministic_router, Router):
            raise TypeError("deterministic_router must implement Router")
        if not isinstance(model_router, Router):
            raise TypeError("model_router must implement Router")
        if deterministic_router.has_internal_fallback:
            raise ValueError("deterministic_router must not own an internal fallback")
        if not isinstance(router_id, str) or not router_id.strip():
            raise TypeError("router_id must be a non-empty string")
        if router_id != router_id.strip():
            raise ValueError("router_id must not include surrounding whitespace")

        self._deterministic_router = deterministic_router
        self._model_router = model_router
        self._router_id = router_id

    @property
    def router_id(self) -> str:
        return self._router_id

    @property
    def deterministic_router(self) -> Router:
        return self._deterministic_router

    @property
    def model_router(self) -> Router:
        return self._model_router

    async def route(self, context: RoutingContext) -> RouteDecision:
        if not isinstance(context, RoutingContext):
            raise TypeError("context must be RoutingContext")

        try:
            return await self._deterministic_router.route(context)
        except RoutingError as exc:
            if exc.code != ErrorCode.ROUTE_NO_MATCH.value:
                raise
        return await self._model_router.route(context)
