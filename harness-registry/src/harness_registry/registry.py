"""Capability Registry 接口和支持 1:N Provider 的内存实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import RLock

from harness_contracts import (
    CapabilityDescriptor,
    ErrorCode,
    ProviderDescriptor,
    ProviderError,
    RegistryError,
)
from harness_spi import Capability

from .models import (
    CapabilityQuery,
    ProviderQuery,
    ProviderRegistration,
    ResolvedCapability,
    legacy_provider_id,
)


class CapabilityRegistry(ABC):
    """Runtime、Plugin Loader 与未来 Selection 共享的能力目录接口。

    旧的 Capability API 继续保留以兼容 Stage 1/2 调用方。新增 Provider API 用于
    Stage 3A 的 1:N 注册；自定义 Registry 若尚未实现 Provider API，会通过旧 API
    获得单 Provider 的兼容行为。
    """

    @abstractmethod
    def register(self, provider: Capability, *, plugin_id: str) -> ResolvedCapability:
        """Legacy：注册一个 Capability Provider。"""

    @abstractmethod
    def unregister(
        self,
        capability_id: str,
        *,
        plugin_id: str | None = None,
    ) -> ResolvedCapability:
        """Legacy：注销唯一匹配的 Capability Provider。"""

    @abstractmethod
    def get(self, capability_id: str) -> ResolvedCapability | None:
        """Legacy：按 Capability ID 获取唯一 Provider；多匹配时允许实现拒绝。"""

    @abstractmethod
    def list(self, query: CapabilityQuery | None = None) -> tuple[ResolvedCapability, ...]:
        """Legacy：返回匹配 Capability 查询的 Provider 快照。"""

    @abstractmethod
    def resolve(self, query: CapabilityQuery) -> ResolvedCapability:
        """Legacy：解析唯一匹配项；无匹配或多匹配均失败。"""

    def register_provider(
        self,
        provider: Capability,
        *,
        descriptor: ProviderDescriptor,
    ) -> ProviderRegistration:
        """注册 Provider；默认适配旧 Registry 的单 Provider 能力。"""

        capability = provider.descriptor()
        expected_id = legacy_provider_id(descriptor.plugin_id, capability.id)
        if descriptor.provider_id != expected_id:
            raise ProviderError(
                "registry does not support explicit provider identity",
                code=ErrorCode.PROVIDER_SELECTION_FAILED,
                details={"provider_id": descriptor.provider_id},
            )
        resolved = self.register(provider, plugin_id=descriptor.plugin_id)
        return ProviderRegistration(
            descriptor=descriptor,
            capability=resolved.descriptor,
            provider=resolved.provider,
        )

    def unregister_provider(self, provider_id: str) -> ProviderRegistration:
        """按 Provider ID 注销；默认通过旧 Registry 快照反查。"""

        registration = self.get_provider(provider_id)
        if registration is None:
            raise ProviderError(
                f"provider not found: {provider_id}",
                code=ErrorCode.PROVIDER_NOT_FOUND,
                details={"provider_id": provider_id},
            )
        self.unregister(
            registration.capability.id,
            plugin_id=registration.descriptor.plugin_id,
        )
        return registration

    def get_provider(self, provider_id: str) -> ProviderRegistration | None:
        """按 Provider ID 获取注册项。"""

        for resolved in self.list():
            registration = _registration_from_legacy(resolved)
            if registration.provider_id == provider_id:
                return registration
        return None

    def candidates(
        self,
        capability_id: str,
        *,
        plugin_id: str | None = None,
    ) -> tuple[ProviderRegistration, ...]:
        """返回某 Capability 的 Provider 候选；Registry 不做选择。"""

        query = CapabilityQuery(id=capability_id, plugin_id=plugin_id)
        return tuple(_registration_from_legacy(item) for item in self.list(query))

    def list_providers(
        self,
        query: ProviderQuery | None = None,
    ) -> tuple[ProviderRegistration, ...]:
        """返回 Provider 快照。"""

        effective_query = query or ProviderQuery()
        registrations = tuple(_registration_from_legacy(item) for item in self.list())
        matches = (item for item in registrations if _matches_provider(item, effective_query))
        return tuple(sorted(matches, key=_provider_sort_key))

    def get_capability_descriptor(self, capability_id: str) -> CapabilityDescriptor | None:
        """返回 canonical CapabilityDescriptor，不暴露 Provider。"""

        candidates = self.candidates(capability_id)
        return candidates[0].capability if candidates else None

    def list_capability_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        """返回按 Capability ID 去重的 canonical Descriptor 快照。"""

        descriptors: dict[str, CapabilityDescriptor] = {}
        for registration in self.list_providers():
            descriptors.setdefault(registration.capability.id, registration.capability)
        return tuple(descriptors[key] for key in sorted(descriptors))


class InMemoryCapabilityRegistry(CapabilityRegistry):
    """线程安全的单进程 1:N Provider Registry。"""

    def __init__(self) -> None:
        self._providers_by_id: dict[str, ProviderRegistration] = {}
        self._providers_by_capability: dict[str, list[str]] = {}
        self._capability_descriptors: dict[str, CapabilityDescriptor] = {}
        self._legacy_resolved_by_provider_id: dict[str, ResolvedCapability] = {}
        self._lock = RLock()

    def register(self, provider: Capability, *, plugin_id: str) -> ResolvedCapability:
        if not isinstance(provider, Capability):
            raise RegistryError(
                "provider must implement Capability",
                code="HARNESS.REGISTRY.INVALID_PROVIDER",
            )
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise RegistryError(
                "plugin_id must not be empty",
                code="HARNESS.REGISTRY.INVALID_PLUGIN_ID",
            )
        capability = provider.descriptor()
        registration = self.register_provider(
            provider,
            descriptor=ProviderDescriptor(
                provider_id=legacy_provider_id(plugin_id, capability.id),
                capability_id=capability.id,
                plugin_id=plugin_id,
                implementation_version=capability.version,
                metadata={"identity_source": "legacy-registry"},
            ),
        )
        with self._lock:
            return self._legacy_resolved_by_provider_id[registration.provider_id]

    def register_provider(
        self,
        provider: Capability,
        *,
        descriptor: ProviderDescriptor,
    ) -> ProviderRegistration:
        if not isinstance(provider, Capability):
            raise ProviderError(
                "provider must implement Capability",
                code=ErrorCode.PROVIDER_EXECUTION_FAILED,
            )
        if not isinstance(descriptor, ProviderDescriptor):
            raise TypeError("descriptor must be ProviderDescriptor")

        capability = provider.descriptor()
        if descriptor.capability_id != capability.id:
            raise ProviderError(
                "provider descriptor capability_id does not match CapabilityDescriptor",
                code=ErrorCode.PROVIDER_CAPABILITY_MISMATCH,
                details={
                    "provider_id": descriptor.provider_id,
                    "provider_capability_id": descriptor.capability_id,
                    "capability_id": capability.id,
                },
            )

        registration = ProviderRegistration(
            descriptor=descriptor,
            capability=capability,
            provider=provider,
        )
        with self._lock:
            if descriptor.provider_id in self._providers_by_id:
                raise ProviderError(
                    f"provider already registered: {descriptor.provider_id}",
                    code=ErrorCode.PROVIDER_DUPLICATE,
                    details={"provider_id": descriptor.provider_id},
                )

            canonical = self._capability_descriptors.get(capability.id)
            if canonical is not None and not _capability_contract_compatible(
                canonical,
                capability,
            ):
                raise ProviderError(
                    "provider capability contract does not match canonical descriptor: "
                    f"{capability.id}",
                    code=ErrorCode.PROVIDER_CAPABILITY_MISMATCH,
                    details={
                        "provider_id": descriptor.provider_id,
                        "capability_id": capability.id,
                    },
                )

            if canonical is None:
                self._capability_descriptors[capability.id] = capability
            self._providers_by_id[descriptor.provider_id] = registration
            self._legacy_resolved_by_provider_id[descriptor.provider_id] = (
                _resolved_from_registration(registration)
            )
            self._providers_by_capability.setdefault(capability.id, []).append(
                descriptor.provider_id
            )
        return registration

    def unregister(
        self,
        capability_id: str,
        *,
        plugin_id: str | None = None,
    ) -> ResolvedCapability:
        matches = self.candidates(capability_id, plugin_id=plugin_id)
        if not matches:
            all_candidates = self.candidates(capability_id)
            if plugin_id is not None and all_candidates:
                raise RegistryError(
                    f"capability is owned by another plugin: {capability_id}",
                    code="HARNESS.REGISTRY.OWNER_MISMATCH",
                    details={
                        "capability_id": capability_id,
                        "expected_plugin_id": plugin_id,
                        "actual_plugin_ids": sorted({item.plugin_id for item in all_candidates}),
                    },
                )
            raise RegistryError(
                f"capability not found: {capability_id}",
                details={"capability_id": capability_id},
            )
        if len(matches) > 1:
            raise RegistryError(
                f"multiple providers match capability: {capability_id}",
                code="HARNESS.REGISTRY.AMBIGUOUS",
                details={
                    "capability_id": capability_id,
                    "plugin_id": plugin_id,
                    "provider_ids": [item.provider_id for item in matches],
                },
            )
        provider_id = matches[0].provider_id
        with self._lock:
            resolved = self._legacy_resolved_by_provider_id[provider_id]
        self.unregister_provider(provider_id)
        return resolved

    def unregister_provider(self, provider_id: str) -> ProviderRegistration:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise TypeError("provider_id must be a non-empty string")
        with self._lock:
            registration = self._providers_by_id.get(provider_id)
            if registration is None:
                raise ProviderError(
                    f"provider not found: {provider_id}",
                    code=ErrorCode.PROVIDER_NOT_FOUND,
                    details={"provider_id": provider_id},
                )

            del self._providers_by_id[provider_id]
            del self._legacy_resolved_by_provider_id[provider_id]
            capability_id = registration.capability.id
            provider_ids = self._providers_by_capability[capability_id]
            provider_ids.remove(provider_id)
            if not provider_ids:
                del self._providers_by_capability[capability_id]
                del self._capability_descriptors[capability_id]
        return registration

    def get(self, capability_id: str) -> ResolvedCapability | None:
        matches = self.candidates(capability_id)
        if not matches:
            return None
        if len(matches) > 1:
            raise RegistryError(
                f"multiple providers match capability: {capability_id}",
                code="HARNESS.REGISTRY.AMBIGUOUS",
                details={
                    "capability_id": capability_id,
                    "provider_ids": [item.provider_id for item in matches],
                },
            )
        with self._lock:
            return self._legacy_resolved_by_provider_id[matches[0].provider_id]

    def get_provider(self, provider_id: str) -> ProviderRegistration | None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise TypeError("provider_id must be a non-empty string")
        with self._lock:
            return self._providers_by_id.get(provider_id)

    def candidates(
        self,
        capability_id: str,
        *,
        plugin_id: str | None = None,
    ) -> tuple[ProviderRegistration, ...]:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise TypeError("capability_id must be a non-empty string")
        if plugin_id is not None and (not isinstance(plugin_id, str) or not plugin_id.strip()):
            raise TypeError("plugin_id must be a non-empty string when provided")

        with self._lock:
            provider_ids = tuple(self._providers_by_capability.get(capability_id, ()))
            registrations = tuple(self._providers_by_id[item] for item in provider_ids)
        if plugin_id is not None:
            registrations = tuple(
                item for item in registrations if item.descriptor.plugin_id == plugin_id
            )
        return tuple(sorted(registrations, key=lambda item: item.provider_id))

    def list(self, query: CapabilityQuery | None = None) -> tuple[ResolvedCapability, ...]:
        effective_query = query or CapabilityQuery()
        with self._lock:
            registrations = tuple(self._providers_by_id.values())
        matches = (
            item for item in registrations if _matches_capability_query(item, effective_query)
        )
        ordered = tuple(sorted(matches, key=_provider_sort_key))
        with self._lock:
            return tuple(self._legacy_resolved_by_provider_id[item.provider_id] for item in ordered)

    def list_providers(
        self,
        query: ProviderQuery | None = None,
    ) -> tuple[ProviderRegistration, ...]:
        effective_query = query or ProviderQuery()
        with self._lock:
            registrations = tuple(self._providers_by_id.values())
        matches = (item for item in registrations if _matches_provider(item, effective_query))
        return tuple(sorted(matches, key=_provider_sort_key))

    def resolve(self, query: CapabilityQuery) -> ResolvedCapability:
        matches = self.list(query)
        if not matches:
            raise RegistryError(
                "no capability matches query",
                details={"query": query.model_dump(mode="json")},
            )
        if len(matches) > 1:
            raise RegistryError(
                "multiple providers match capability query",
                code="HARNESS.REGISTRY.AMBIGUOUS",
                details={
                    "query": query.model_dump(mode="json"),
                    "capability_ids": [item.descriptor.id for item in matches],
                    "provider_ids": [item.provider_id for item in matches],
                },
            )
        return matches[0]

    def get_capability_descriptor(self, capability_id: str) -> CapabilityDescriptor | None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise TypeError("capability_id must be a non-empty string")
        with self._lock:
            return self._capability_descriptors.get(capability_id)

    def list_capability_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        with self._lock:
            descriptors = tuple(self._capability_descriptors.values())
        return tuple(sorted(descriptors, key=lambda item: item.id))


def _registration_from_legacy(resolved: ResolvedCapability) -> ProviderRegistration:
    provider_id = resolved.provider_id or legacy_provider_id(
        resolved.plugin_id,
        resolved.descriptor.id,
    )
    return ProviderRegistration(
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=resolved.descriptor.id,
            plugin_id=resolved.plugin_id,
            implementation_version=resolved.descriptor.version,
            metadata={"identity_source": "legacy-registry-adapter"},
        ),
        capability=resolved.descriptor,
        provider=resolved.provider,
    )


def _resolved_from_registration(registration: ProviderRegistration) -> ResolvedCapability:
    return ResolvedCapability(
        descriptor=registration.capability,
        plugin_id=registration.descriptor.plugin_id,
        provider=registration.provider,
        provider_id=registration.descriptor.provider_id,
    )


def _matches_capability_query(
    registration: ProviderRegistration,
    query: CapabilityQuery,
) -> bool:
    descriptor = registration.capability
    return (
        (query.id is None or descriptor.id == query.id)
        and (query.type is None or descriptor.type is query.type)
        and (query.version is None or descriptor.version == query.version)
        and (query.plugin_id is None or registration.descriptor.plugin_id == query.plugin_id)
        and query.tags.issubset(descriptor.tags)
    )


def _matches_provider(
    registration: ProviderRegistration,
    query: ProviderQuery,
) -> bool:
    descriptor = registration.descriptor
    capability = registration.capability
    return (
        (query.provider_id is None or descriptor.provider_id == query.provider_id)
        and (query.capability_id is None or capability.id == query.capability_id)
        and (query.plugin_id is None or descriptor.plugin_id == query.plugin_id)
        and (query.capability_type is None or capability.type is query.capability_type)
        and (query.capability_version is None or capability.version == query.capability_version)
        and (query.region is None or descriptor.region == query.region)
        and query.provider_tags.issubset(descriptor.tags)
    )


def _capability_contract_compatible(
    canonical: CapabilityDescriptor,
    candidate: CapabilityDescriptor,
) -> bool:
    """只比较执行/Schema 契约；Provider 展示标签和 metadata 不参与兼容性。"""

    return (
        canonical.id == candidate.id
        and canonical.type is candidate.type
        and canonical.version == candidate.version
        and canonical.input_schema == candidate.input_schema
        and canonical.output_schema == candidate.output_schema
        and canonical.execution_profile == candidate.execution_profile
    )


def _provider_sort_key(registration: ProviderRegistration) -> tuple[str, str]:
    return registration.capability.id, registration.provider_id
