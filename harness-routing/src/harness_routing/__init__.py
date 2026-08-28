"""FinanceClaw 的无执行权路由基础。"""

from typing import TYPE_CHECKING, Any

from .models import RequestSummary, RoutePolicyConstraints, RoutingContext
from .projection import RequestProjector, SafeRequestProjector
from .router import Router
from .rules import InputTypeRouteRule, RuleRouter
from .validation import RouteDecisionValidator

if TYPE_CHECKING:
    from .llm import LLMRouter

__all__ = [
    "InputTypeRouteRule",
    "LLMRouter",
    "RequestProjector",
    "RequestSummary",
    "RouteDecisionValidator",
    "RoutePolicyConstraints",
    "Router",
    "RoutingContext",
    "RuleRouter",
    "SafeRequestProjector",
]


def __getattr__(name: str) -> Any:
    """延迟加载依赖 ModelGateway 的 Router，避免 Policy/Runtime 反向初始化环。"""

    if name == "LLMRouter":
        from .llm import LLMRouter

        return LLMRouter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
