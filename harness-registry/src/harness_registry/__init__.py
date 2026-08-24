"""Capability 的注册、查询与解析。

该包只管理能力目录，不承担本地插件发现，
也不向业务代码暴露通用 Service Locator。
"""

from .models import CapabilityQuery, ResolvedCapability
from .registry import CapabilityRegistry, InMemoryCapabilityRegistry

__all__ = [
    "CapabilityQuery",
    "CapabilityRegistry",
    "InMemoryCapabilityRegistry",
    "ResolvedCapability",
]
