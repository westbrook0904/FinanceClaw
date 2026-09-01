"""FinanceClaw Harness 的默认 Composition Root。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_agentic import (
    ExplorationEngine,
    ExplorationPlanFactory,
    ExplorationProfileMaterializer,
    ScopedActionExecutor,
)
from harness_context import (
    CapabilityCatalogContextSource,
    ContextPipeline,
    ContextPolicy,
    MemoryContextSource,
    ObservationContextSource,
    RequestContextSource,
)
from harness_contracts import ExplorationProfile
from harness_events import EventPublisher, InMemoryEventBus
from harness_execution import BasicScheduler, ExecutionEngine
from harness_memory import MemoryGateway, MemoryPolicy, MemoryProvider
from harness_model import ModelGateway, StructuredGenerationAdapter
from harness_planning import (
    PlanIdentityFactory,
    PlanMaterializer,
    Planner,
    PlannerOutputNormalizer,
    PlannerRegistry,
    PlanValidator,
)
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
    context_pipeline: ContextPipeline | None = None,
    memory_provider: MemoryProvider | None = None,
    memory_gateway: MemoryGateway | None = None,
    memory_namespaces: Iterable[str] = (),
    tracer: Tracer | None = None,
    provider_selector: ProviderSelector | None = None,
    context_factory: InvocationContextFactory | None = None,
    capability_catalog: CapabilityCatalog | None = None,
    plan_validator: PlanValidator | None = None,
    plan_identity_factory: PlanIdentityFactory | None = None,
    state_store: StateStore | None = None,
    event_publisher: EventPublisher | None = None,
    router: Router | None = None,
    planners: Iterable[Planner] = (),
    default_planner_id: str | None = None,
    exploration_profiles: Iterable[ExplorationProfile] = (),
    default_explorer_id: str | None = None,
    single_writer_guaranteed: bool = False,
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
    if context_pipeline is not None and not isinstance(context_pipeline, ContextPipeline):
        raise TypeError("context_pipeline must be ContextPipeline")
    if memory_provider is not None and not isinstance(memory_provider, MemoryProvider):
        raise TypeError("memory_provider must implement MemoryProvider")
    if memory_gateway is not None and not isinstance(memory_gateway, MemoryGateway):
        raise TypeError("memory_gateway must be MemoryGateway")
    if memory_provider is not None and memory_gateway is not None:
        raise ValueError("memory_provider and memory_gateway cannot be configured together")
    configured_memory_namespaces = _memory_namespaces(memory_namespaces)
    if provider_selector is not None and not isinstance(provider_selector, ProviderSelector):
        raise TypeError("provider_selector must implement ProviderSelector")
    if capability_catalog is not None and not isinstance(capability_catalog, CapabilityCatalog):
        raise TypeError("capability_catalog must implement CapabilityCatalog")
    if plan_validator is not None and not isinstance(plan_validator, PlanValidator):
        raise TypeError("plan_validator must be PlanValidator")
    if plan_identity_factory is not None and not isinstance(
        plan_identity_factory,
        PlanIdentityFactory,
    ):
        raise TypeError("plan_identity_factory must be PlanIdentityFactory")
    if state_store is not None and not isinstance(state_store, StateStore):
        raise TypeError("state_store must implement StateStore")
    if event_publisher is not None and not isinstance(event_publisher, EventPublisher):
        raise TypeError("event_publisher must implement EventPublisher")
    if router is not None and not isinstance(router, Router):
        raise TypeError("router must implement Router")
    if request_projector is not None and not isinstance(request_projector, RequestProjector):
        raise TypeError("request_projector must implement RequestProjector")
    if not isinstance(single_writer_guaranteed, bool):
        raise TypeError("single_writer_guaranteed must be bool")
    if isinstance(exploration_profiles, ExplorationProfile | str):
        raise TypeError("exploration_profiles must be an iterable of ExplorationProfile")
    configured_profiles = tuple(exploration_profiles)
    if any(not isinstance(profile, ExplorationProfile) for profile in configured_profiles):
        raise TypeError("exploration_profiles must contain ExplorationProfile values")
    profile_ids = [profile.profile_id for profile in configured_profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ValueError("exploration profile IDs must be unique")
    if configured_profiles and not single_writer_guaranteed:
        raise ValueError("exploration_profiles require explicit single_writer_guaranteed=True")
    if default_explorer_id is not None and (
        not isinstance(default_explorer_id, str) or not default_explorer_id.strip()
    ):
        raise TypeError("default_explorer_id must be a non-empty string when provided")
    if default_explorer_id is not None and default_explorer_id not in profile_ids:
        raise ValueError("default_explorer_id must reference a configured profile")
    if len(configured_profiles) > 1 and default_explorer_id is None and router is None:
        raise ValueError("multiple exploration profiles require default_explorer_id")
    effective_default_explorer_id = default_explorer_id or (
        configured_profiles[0].profile_id if len(configured_profiles) == 1 else None
    )
    exploration_available = bool(configured_profiles and single_writer_guaranteed)
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
    if memory_provider is not None:
        if not configured_memory_namespaces:
            raise ValueError("memory_namespaces are required with memory_provider")
        effective_memory_gateway = MemoryGateway(
            memory_provider,
            MemoryPolicy(effective_policy_engine),
            allowed_namespaces=configured_memory_namespaces,
        )
    else:
        effective_memory_gateway = memory_gateway
    if effective_memory_gateway is None:
        if configured_memory_namespaces:
            raise ValueError("memory_namespaces require memory_provider or memory_gateway")
        memory_context_namespaces = frozenset()
    else:
        if effective_memory_gateway.policy.policy_engine is not effective_policy_engine:
            raise ValueError("memory_gateway and harness must use the same policy_engine")
        memory_context_namespaces = (
            configured_memory_namespaces or effective_memory_gateway.allowed_namespaces
        )
        if not memory_context_namespaces.issubset(effective_memory_gateway.allowed_namespaces):
            raise ValueError("memory_namespaces must be allowed by memory_gateway")

    if context_pipeline is None:
        context_sources = [
            RequestContextSource(),
            CapabilityCatalogContextSource(),
            ObservationContextSource(),
        ]
        if effective_memory_gateway is not None:
            context_sources.append(
                MemoryContextSource(
                    effective_memory_gateway,
                    namespaces=memory_context_namespaces,
                )
            )
        effective_context_pipeline = ContextPipeline(
            ContextPolicy(effective_policy_engine),
            sources=context_sources,
        )
    else:
        effective_context_pipeline = context_pipeline
        if effective_memory_gateway is not None and not any(
            isinstance(source, MemoryContextSource) and source.gateway is effective_memory_gateway
            for source in context_pipeline.sources
        ):
            raise ValueError("custom context_pipeline must include the configured memory_gateway")
        if exploration_available and not any(
            isinstance(source, ObservationContextSource) for source in context_pipeline.sources
        ):
            raise ValueError(
                "exploration-enabled custom context_pipeline must include ObservationContextSource"
            )
    if effective_context_pipeline.policy.policy_engine is not effective_policy_engine:
        raise ValueError("context_pipeline and harness must use the same policy_engine")
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
    if plan_validator is not None and (
        plan_validator.exploration_available is not exploration_available
    ):
        raise ValueError("plan_validator.exploration_available must match exploration composition")
    effective_plan_validator = plan_validator or PlanValidator(
        effective_catalog,
        exploration_available=exploration_available,
    )
    effective_state_store = state_store or InMemoryStateStore()
    effective_event_publisher = event_publisher or InMemoryEventBus()
    planner_output_normalizer = PlannerOutputNormalizer()
    plan_materializer = PlanMaterializer(plan_identity_factory)
    planner_registry = PlannerRegistry(planners)
    if default_planner_id is not None:
        planner_registry.get(default_planner_id)
    effective_router = (
        router
        if router is not None
        else RuleRouter(
            **(
                {"explorer_id": effective_default_explorer_id}
                if effective_default_explorer_id is not None
                else {}
            )
        )
    )
    effective_request_projector = (
        request_projector if request_projector is not None else SafeRequestProjector()
    )
    route_decision_validator = RouteDecisionValidator(exploration_available=exploration_available)
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
    exploration_profile_materializer = (
        ExplorationProfileMaterializer(effective_catalog) if exploration_available else None
    )
    exploration_plan_factory = (
        ExplorationPlanFactory(
            exploration_profile_materializer,
            validator=effective_plan_validator,
        )
        if exploration_profile_materializer is not None
        else None
    )
    scoped_action_executor = (
        ScopedActionExecutor(
            effective_catalog,
            invoker,
            effective_tracer,
            lifecycle,
        )
        if exploration_available
        else None
    )
    exploration_engine = (
        ExplorationEngine(
            StructuredGenerationAdapter(model_gateway),
            effective_context_pipeline,
            effective_catalog,
            scoped_action_executor,
            effective_tracer,
            lifecycle,
            memory_available=any(
                isinstance(source, MemoryContextSource)
                for source in effective_context_pipeline.sources
            ),
        )
        if scoped_action_executor is not None
        else None
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
        exploration_engine=exploration_engine,
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
        effective_context_pipeline,
        effective_catalog,
        invoker,
        execution_engine,
        lifecycle,
        effective_tracer,
        effective_event_publisher,
        planner_registry,
        default_planner_id,
        planner_output_normalizer,
        plan_materializer,
        {profile.profile_id: profile for profile in configured_profiles},
        exploration_plan_factory,
    )

    return HarnessApplication(
        HarnessComponents(
            registry=effective_registry,
            policy_engine=effective_policy_engine,
            context_pipeline=effective_context_pipeline,
            memory_provider=(
                effective_memory_gateway.provider if effective_memory_gateway is not None else None
            ),
            memory_gateway=effective_memory_gateway,
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
            planner_output_normalizer=planner_output_normalizer,
            plan_materializer=plan_materializer,
            exploration_profile_materializer=exploration_profile_materializer,
            exploration_plan_factory=exploration_plan_factory,
            scoped_action_executor=scoped_action_executor,
            exploration_engine=exploration_engine,
            single_writer_guaranteed=single_writer_guaranteed,
        )
    )


def _memory_namespaces(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, str):
        raise TypeError("memory_namespaces must be an iterable of strings")
    namespaces = frozenset(values)
    if any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in namespaces
    ):
        raise TypeError("memory_namespaces must contain non-empty trimmed strings")
    return namespaces
