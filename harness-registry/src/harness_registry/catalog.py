"""不暴露 Provider 实例的只读 Capability Catalog。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import CapabilityDescriptor

from .registry import CapabilityRegistry


class CapabilityCatalog(ABC):
    """顶层 Agent、Workflow 与工具管理层可见的最小只读能力目录。"""

    @abstractmethod
    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        """按稳定 ID 返回 Descriptor，不存在时返回 ``None``。"""

    @abstractmethod
    def list(self) -> tuple[CapabilityDescriptor, ...]:
        """返回按 Capability ID 排序的不可变描述快照。"""


class RegistryCapabilityCatalog(CapabilityCatalog):
    """把 Provider Registry 投影成 capability-only 的 canonical 只读视图。"""

    def __init__(self, registry: CapabilityRegistry) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must implement CapabilityRegistry")
        self._registry = registry

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise TypeError("capability_id must be a non-empty string")
        return self._registry.get_capability_descriptor(capability_id)

    def list(self) -> tuple[CapabilityDescriptor, ...]:
        return self._registry.list_capability_descriptors()
