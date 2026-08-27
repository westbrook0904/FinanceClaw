"""Capability 与 Provider 的注册、查询和只读 Catalog。"""

from .catalog import CapabilityCatalog, RegistryCapabilityCatalog
from .models import (
    CapabilityQuery,
    ProviderQuery,
    ProviderRegistration,
    ResolvedCapability,
    legacy_provider_id,
)
from .registry import CapabilityRegistry, InMemoryCapabilityRegistry

__all__ = [
    "CapabilityCatalog",
    "CapabilityQuery",
    "CapabilityRegistry",
    "InMemoryCapabilityRegistry",
    "ProviderQuery",
    "ProviderRegistration",
    "RegistryCapabilityCatalog",
    "ResolvedCapability",
    "legacy_provider_id",
]
