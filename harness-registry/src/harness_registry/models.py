"""Capability / Provider Registry 的查询与稳定结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ContractModel,
    ProviderDescriptor,
)
from harness_spi import Capability
from pydantic import Field

NonEmptyString = Annotated[str, Field(min_length=1)]


class CapabilityQuery(ContractModel):
    """Legacy Capability 查询条件；所有已提供条件采用 AND 语义。"""

    id: NonEmptyString | None = None
    type: CapabilityType | None = None
    tags: frozenset[str] = Field(default_factory=frozenset)
    version: NonEmptyString | None = None
    plugin_id: NonEmptyString | None = None


class ProviderQuery(ContractModel):
    """Provider Registry 的业务无关查询条件。"""

    provider_id: NonEmptyString | None = None
    capability_id: NonEmptyString | None = None
    plugin_id: NonEmptyString | None = None
    capability_type: CapabilityType | None = None
    capability_version: NonEmptyString | None = None
    provider_tags: frozenset[str] = Field(default_factory=frozenset)
    region: NonEmptyString | None = None


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """Provider 身份、Capability 语义以及受信任可调用实例。"""

    descriptor: ProviderDescriptor
    capability: CapabilityDescriptor
    provider: Capability

    @property
    def provider_id(self) -> str:
        return self.descriptor.provider_id

    @property
    def plugin_id(self) -> str:
        return self.descriptor.plugin_id


@dataclass(frozen=True, slots=True)
class ResolvedCapability:
    """Legacy Runtime 使用的唯一 Capability 解析结果。"""

    descriptor: CapabilityDescriptor
    plugin_id: str
    provider: Capability
    provider_id: str | None = None


def legacy_provider_id(plugin_id: str, capability_id: str) -> str:
    """为没有显式 Provider Identity 的旧插件生成稳定且无歧义的 ID。"""

    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise TypeError("plugin_id must be a non-empty string")
    if not isinstance(capability_id, str) or not capability_id.strip():
        raise TypeError("capability_id must be a non-empty string")
    encoded_plugin = quote(plugin_id.strip(), safe="")
    encoded_capability = quote(capability_id.strip(), safe="/")
    return f"{encoded_plugin}:{encoded_capability}"
