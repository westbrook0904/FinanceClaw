"""FinanceClaw 核心能力的默认 Composition Root。"""

from __future__ import annotations

from collections.abc import Iterable

from harness_context import (
    CapabilityCatalogContextSource,
    ContextPipeline,
    ContextPolicy,
    MemoryContextSource,
    ObservationContextSource,
    RequestContextSource,
)
from harness_events import EventPublisher, InMemoryEventBus
from harness_memory import MemoryGateway, MemoryPolicy, MemoryProvider
from harness_plugin_local import LocalPluginLoader, LocalPluginProvider
from harness_policy import AllowAllPolicy, Policy, PolicyEngine
from harness_registry import (
    CapabilityCatalog,
    CapabilityRegistry,
    InMemoryCapabilityRegistry,
    RegistryCapabilityCatalog,
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
from harness_trace import InMemoryTracer, Tracer

from .application import HarnessApplication, HarnessComponents


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
    event_publisher: EventPublisher | None = None,
    plugin_provider: LocalPluginProvider | None = None,
    entry_point_group: str | None = "financeclaw.plugins",
) -> HarnessApplication:
    """组装 FinanceClaw 领域核心，不创建模型、路由、Planner 或图执行器。"""

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
    if provider_selector is not None and not isinstance(provider_selector, ProviderSelector):
        raise TypeError("provider_selector must implement ProviderSelector")
    if capability_catalog is not None and not isinstance(capability_catalog, CapabilityCatalog):
        raise TypeError("capability_catalog must implement CapabilityCatalog")
    if event_publisher is not None and not isinstance(event_publisher, EventPublisher):
        raise TypeError("event_publisher must implement EventPublisher")

    configured_namespaces = _memory_namespaces(memory_namespaces)
    effective_registry = registry or InMemoryCapabilityRegistry()
    effective_policy_engine = policy_engine or PolicyEngine(
        tuple(policies) if policies is not None else (AllowAllPolicy(),)
    )

    if memory_provider is not None:
        if not configured_namespaces:
            raise ValueError("memory_namespaces are required with memory_provider")
        effective_memory_gateway = MemoryGateway(
            memory_provider,
            MemoryPolicy(effective_policy_engine),
            allowed_namespaces=configured_namespaces,
        )
    else:
        effective_memory_gateway = memory_gateway

    if effective_memory_gateway is None:
        if configured_namespaces:
            raise ValueError("memory_namespaces require memory_provider or memory_gateway")
        context_namespaces = frozenset()
    else:
        if effective_memory_gateway.policy.policy_engine is not effective_policy_engine:
            raise ValueError("memory_gateway and harness must use the same policy_engine")
        context_namespaces = configured_namespaces or effective_memory_gateway.allowed_namespaces
        if not context_namespaces.issubset(effective_memory_gateway.allowed_namespaces):
            raise ValueError("memory_namespaces must be allowed by memory_gateway")

    effective_catalog = capability_catalog or RegistryCapabilityCatalog(effective_registry)
    if context_pipeline is None:
        sources = [
            RequestContextSource(),
            CapabilityCatalogContextSource(),
            ObservationContextSource(),
        ]
        if effective_memory_gateway is not None:
            sources.append(
                MemoryContextSource(
                    effective_memory_gateway,
                    namespaces=context_namespaces,
                )
            )
        effective_context_pipeline = ContextPipeline(
            ContextPolicy(effective_policy_engine),
            sources=sources,
        )
    else:
        effective_context_pipeline = context_pipeline
        if effective_memory_gateway is not None and not any(
            isinstance(source, MemoryContextSource) and source.gateway is effective_memory_gateway
            for source in context_pipeline.sources
        ):
            raise ValueError("custom context_pipeline must include the configured memory_gateway")
    if effective_context_pipeline.policy.policy_engine is not effective_policy_engine:
        raise ValueError("context_pipeline and harness must use the same policy_engine")

    effective_tracer = tracer or InMemoryTracer()
    effective_selector = provider_selector or PrioritySelector()
    effective_context_factory = context_factory or DefaultInvocationContextFactory()
    effective_events = event_publisher or InMemoryEventBus()
    provider = plugin_provider or LocalPluginProvider(
        explicit_plugins,
        entry_point_group=entry_point_group,
    )
    plugin_loader = LocalPluginLoader(effective_registry, provider)
    lifecycle = InvocationLifecycle(
        effective_tracer,
        context_factory=effective_context_factory,
    )
    invoker = CapabilityInvoker(
        effective_registry,
        effective_policy_engine,
        effective_tracer,
        lifecycle=lifecycle,
        provider_selector=effective_selector,
        event_publisher=effective_events,
    )
    runtime = HarnessRuntime(
        effective_registry,
        effective_policy_engine,
        effective_tracer,
        lifecycle=lifecycle,
        invoker=invoker,
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
            provider_selector=effective_selector,
            plugin_loader=plugin_loader,
            context_factory=effective_context_factory,
            capability_catalog=effective_catalog,
            lifecycle=lifecycle,
            invoker=invoker,
            runtime=runtime,
            event_publisher=effective_events,
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
