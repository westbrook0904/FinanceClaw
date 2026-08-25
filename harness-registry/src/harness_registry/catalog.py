"""不暴露 Provider 实例的只读 Capability Catalog。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_contracts import CapabilityDescriptor

from .registry import CapabilityRegistry


class CapabilityCatalog(ABC):
    """Planner 和 PlanValidator 可见的最小只读能力目录。"""

    @abstractmethod
    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        """按稳定 ID 返回 Descriptor，不存在时返回 ``None``。"""

    @abstractmethod
    def list(self) -> tuple[CapabilityDescriptor, ...]:
        """返回按 Capability ID 排序的不可变描述快照。"""


class RegistryCapabilityCatalog(CapabilityCatalog):
    """把 CapabilityRegistry 投影成只包含 Descriptor 的只读视图。"""

    def __init__(self, registry: CapabilityRegistry) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must implement CapabilityRegistry")
        self._registry = registry

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise TypeError("capability_id must be a non-empty string")
        resolved = self._registry.get(capability_id)
        return resolved.descriptor if resolved is not None else None

    def list(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item.descriptor for item in self._registry.list())
