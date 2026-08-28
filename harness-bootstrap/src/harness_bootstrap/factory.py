"""FinanceClaw Harness 的默认 Composition Root。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_events import EventPublisher, InMemoryEventBus
from harness_execution import BasicScheduler, ExecutionEngine
from harness_model import ModelGateway
from harness_planning import Planner, PlannerRegistry, PlanValidator
from harness_plugin_local import LocalPluginLoader, LocalPluginProvider
from harness_policy import AllowAllPolicy, Policy, PolicyEngine
from harness_registry import (
    CapabilityCatalog,
    CapabilityRegistry,
    InMemoryCapabilityRegistry,
    RegistryCapabilityCatalog,
)
from harness_routing import (
    RequestProjector,
    RouteDecisionValidator,
    Router,
    RuleRouter,
    SafeRequestProjector,
)
from harness_runtime import (
    CapabilityInvoker,
    DefaultInvocationContextFactory,
    HarnessRuntime,
    InvocationContextFactory,
    InvocationLifecycle,
)
from harness_selection import PrioritySelector, ProviderSelector
from harness_spi import PluginSPI
from harness_state import InMemoryStateStore, StateStore
from harness_trace import InMemoryTracer, Tracer

from .application import HarnessApplication, HarnessComponents
from .coordinator import RequestCoordinator


def build_harness(
    *,
    plugins: Iterable[PluginSPI] = (),
    policies: Iterable[Policy] | None = None,
    registry: CapabilityRegistry | None = None,
    policy_engine: PolicyEngine | None = None,
    tracer: Tracer | None = None,
    provider_selector: ProviderSelector | None = None,
    context_factory: InvocationContextFactory | None = None,
    capability_catalog: CapabilityCatalog | None = None,
    plan_validator: PlanValidator | None = None,
    state_store: StateStore | None = None,
    event_publisher: EventPublisher | None = None,
    router: Router | None = None,
    planners: Iterable[Planner] = (),
    default_planner_id: str | None = None,
    request_projector: RequestProjector | None = None,
    plugin_provider: LocalPluginProvider | None = None,
    entry_point_group: str | None = "financeclaw.plugins",
) -> HarnessApplication:
    """组装 Harness，但不自动执行插件发现或初始化。

    默认实现使用内存 Registry、显式 ``AllowAllPolicy``、``InMemoryTracer``、
    ``DefaultInvocationContextFactory``、只读 CapabilityCatalog、PlanValidator、
    ``InMemoryStateStore`` 与 ``LocalPluginProvider``。调用方可以从 Composition
    Root 替换这些实现，而无需修改 Runtime 或业务插件。
    """

    explicit_plugins = tuple(plugins)
    if plugin_provider is not None and explicit_plugins:
        raise ValueError("plugins and plugin_provider cannot be configured together")
    if policy_engine is not None and policies is not None:
        raise ValueError("policies and policy_engine cannot be configured together")
    if provider_selector is not None and not isinstance(provider_selector, ProviderSelector):
        raise TypeError("provider_selector must implement ProviderSelector")
    if capability_catalog is not None and not isinstance(capability_catalog, CapabilityCatalog):
        raise TypeError("capability_catalog must implement CapabilityCatalog")
    if plan_validator is not None and not isinstance(plan_validator, PlanValidator):
        raise TypeError("plan_validator must be PlanValidator")
    if state_store is not None and not isinstance(state_store, StateStore):
        raise TypeError("state_store must implement StateStore")
    if event_publisher is not None and not isinstance(event_publisher, EventPublisher):
        raise TypeError("event_publisher must implement EventPublisher")
    if router is not None and not isinstance(router, Router):
        raise TypeError("router must implement Router")
    if request_projector is not None and not isinstance(request_projector, RequestProjector):
        raise TypeError("request_projector must implement RequestProjector")
    if (
        capability_catalog is not None
        and plan_validator is not None
        and plan_validator.catalog is not capability_catalog
    ):
        raise ValueError("capability_catalog and plan_validator.catalog must match")

    effective_registry = registry if registry is not None else InMemoryCapabilityRegistry()
    effective_policy_engine = (
        policy_engine
        if policy_engine is not None
        else PolicyEngine(tuple(policies) if policies is not None else (AllowAllPolicy(),))
    )
    effective_tracer = tracer if tracer is not None else InMemoryTracer()
    effective_provider_selector = (
        provider_selector if provider_selector is not None else PrioritySelector()
    )
    effective_context_factory = (
        context_factory if context_factory is not None else DefaultInvocationContextFactory()
    )
    effective_catalog = (
        capability_catalog
        if capability_catalog is not None
        else (
            plan_validator.catalog
            if plan_validator is not None and plan_validator.catalog is not None
            else RegistryCapabilityCatalog(effective_registry)
        )
    )
    effective_plan_validator = (
        plan_validator if plan_validator is not None else PlanValidator(effective_catalog)
    )
    effective_state_store = state_store or InMemoryStateStore()
    effective_event_publisher = event_publisher or InMemoryEventBus()
    planner_registry = PlannerRegistry(planners)
    if default_planner_id is not None:
        planner_registry.get(default_planner_id)
    effective_router = router if router is not None else RuleRouter()
    effective_request_projector = (
        request_projector if request_projector is not None else SafeRequestProjector()
    )
    route_decision_validator = RouteDecisionValidator()
    effective_provider = (
        plugin_provider
        if plugin_provider is not None
        else LocalPluginProvider(
            explicit_plugins,
            entry_point_group=entry_point_group,
        )
    )
    plugin_loader = LocalPluginLoader(effective_registry, effective_provider)
    lifecycle = InvocationLifecycle(
        effective_tracer,
        context_factory=effective_context_factory,
    )
    invoker = CapabilityInvoker(
        effective_registry,
        effective_policy_engine,
        effective_tracer,
        lifecycle=lifecycle,
        provider_selector=effective_provider_selector,
        event_publisher=effective_event_publisher,
    )
    model_gateway = ModelGateway(
        effective_registry,
        effective_tracer,
        lifecycle=lifecycle,
        provider_selector=effective_provider_selector,
        provider_execution=invoker.provider_execution,
        event_publisher=effective_event_publisher,
    )
    scheduler = BasicScheduler(
        invoker,
        effective_tracer,
        lifecycle,
        capability_catalog=effective_catalog,
    )
    execution_engine = ExecutionEngine(
        effective_plan_validator,
        scheduler,
        invoker,
        effective_tracer,
        lifecycle,
        state_store=effective_state_store,
        event_publisher=effective_event_publisher,
    )
    runtime = HarnessRuntime(
        effective_registry,
        effective_policy_engine,
        effective_tracer,
        lifecycle=lifecycle,
        invoker=invoker,
    )
    request_coordinator = RequestCoordinator(
        effective_router,
        route_decision_validator,
        effective_request_projector,
        effective_policy_engine,
        effective_catalog,
        invoker,
        execution_engine,
        lifecycle,
        effective_tracer,
        planner_registry,
        default_planner_id,
    )

    return HarnessApplication(
        HarnessComponents(
            registry=effective_registry,
            policy_engine=effective_policy_engine,
            tracer=effective_tracer,
            provider_selector=effective_provider_selector,
            plugin_loader=plugin_loader,
            context_factory=effective_context_factory,
            capability_catalog=effective_catalog,
            plan_validator=effective_plan_validator,
            lifecycle=lifecycle,
            invoker=invoker,
            model_gateway=model_gateway,
            runtime=runtime,
            scheduler=scheduler,
            execution_engine=execution_engine,
            state_store=effective_state_store,
            event_publisher=effective_event_publisher,
            router=effective_router,
            request_projector=effective_request_projector,
            route_decision_validator=route_decision_validator,
            request_coordinator=request_coordinator,
            planner_registry=planner_registry,
        )
    )
