"""Registry 接口和阶段一内存实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import RLock

from harness_contracts import RegistryError
from harness_spi import Capability

from .models import CapabilityQuery, ResolvedCapability


class CapabilityRegistry(ABC):
    """Runtime 和 Plugin Loader 共同依赖的能力目录接口。"""

    @abstractmethod
    def register(self, provider: Capability, *, plugin_id: str) -> ResolvedCapability:
        """注册一个能力 Provider。"""

    @abstractmethod
    def unregister(
        self,
        capability_id: str,
        *,
        plugin_id: str | None = None,
    ) -> ResolvedCapability:
        """注销能力；提供 ``plugin_id`` 时同时校验所有权。"""

    @abstractmethod
    def get(self, capability_id: str) -> ResolvedCapability | None:
        """按稳定 ID 获取能力，不存在时返回 ``None``。"""

    @abstractmethod
    def list(self, query: CapabilityQuery | None = None) -> tuple[ResolvedCapability, ...]:
        """返回匹配查询的稳定快照。"""

    @abstractmethod
    def resolve(self, query: CapabilityQuery) -> ResolvedCapability:
        """解析唯一匹配项；无匹配或多匹配均失败。"""


class InMemoryCapabilityRegistry(CapabilityRegistry):
    """线程安全的单进程 Registry。

    阶段一对每个 Capability ID 只允许一个 Provider。后续多 Provider 选择应通过
    新的 Registry 实现或 Provider Selector 演进，不改变 Runtime 的查询方式。
    """

    def __init__(self) -> None:
        self._entries: dict[str, ResolvedCapability] = {}
        self._lock = RLock()

    def register(self, provider: Capability, *, plugin_id: str) -> ResolvedCapability:
        if not isinstance(provider, Capability):
            raise RegistryError(
                "provider must implement Capability",
                code="HARNESS.REGISTRY.INVALID_PROVIDER",
            )
        if not plugin_id or not plugin_id.strip():
            raise RegistryError(
                "plugin_id must not be empty",
                code="HARNESS.REGISTRY.INVALID_PLUGIN_ID",
            )

        descriptor = provider.descriptor()
        entry = ResolvedCapability(
            descriptor=descriptor,
            plugin_id=plugin_id,
            provider=provider,
        )
        with self._lock:
            if descriptor.id in self._entries:
                current = self._entries[descriptor.id]
                raise RegistryError(
                    f"capability already registered: {descriptor.id}",
                    code="HARNESS.REGISTRY.DUPLICATE",
                    details={"capability_id": descriptor.id, "plugin_id": current.plugin_id},
                )
            self._entries[descriptor.id] = entry
        return entry

    def unregister(
        self,
        capability_id: str,
        *,
        plugin_id: str | None = None,
    ) -> ResolvedCapability:
        with self._lock:
            entry = self._entries.get(capability_id)
            if entry is None:
                raise RegistryError(
                    f"capability not found: {capability_id}",
                    details={"capability_id": capability_id},
                )
            if plugin_id is not None and entry.plugin_id != plugin_id:
                raise RegistryError(
                    f"capability is owned by another plugin: {capability_id}",
                    code="HARNESS.REGISTRY.OWNER_MISMATCH",
                    details={
                        "capability_id": capability_id,
                        "expected_plugin_id": plugin_id,
                        "actual_plugin_id": entry.plugin_id,
                    },
                )
            return self._entries.pop(capability_id)

    def get(self, capability_id: str) -> ResolvedCapability | None:
        with self._lock:
            return self._entries.get(capability_id)

    def list(self, query: CapabilityQuery | None = None) -> tuple[ResolvedCapability, ...]:
        effective_query = query or CapabilityQuery()
        with self._lock:
            entries = tuple(self._entries.values())
        matches = (entry for entry in entries if _matches(entry, effective_query))
        return tuple(sorted(matches, key=lambda item: item.descriptor.id))

    def resolve(self, query: CapabilityQuery) -> ResolvedCapability:
        matches = self.list(query)
        if not matches:
            raise RegistryError(
                "no capability matches query",
                details={"query": query.model_dump(mode="json")},
            )
        if len(matches) > 1:
            raise RegistryError(
                "multiple capabilities match query",
                code="HARNESS.REGISTRY.AMBIGUOUS",
                details={
                    "query": query.model_dump(mode="json"),
                    "capability_ids": [item.descriptor.id for item in matches],
                },
            )
        return matches[0]


def _matches(entry: ResolvedCapability, query: CapabilityQuery) -> bool:
    descriptor = entry.descriptor
    return (
        (query.id is None or descriptor.id == query.id)
        and (query.type is None or descriptor.type is query.type)
        and (query.version is None or descriptor.version == query.version)
        and (query.plugin_id is None or entry.plugin_id == query.plugin_id)
        and query.tags.issubset(descriptor.tags)
    )
