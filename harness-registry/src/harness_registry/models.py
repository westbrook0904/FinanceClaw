"""Capability Registry 的查询与解析结果。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from harness_contracts import CapabilityDescriptor, CapabilityType, ContractModel
from harness_spi import Capability


NonEmptyString = Annotated[str, Field(min_length=1)]


class CapabilityQuery(ContractModel):
    """Registry 的业务无关查询条件；所有已提供条件采用 AND 语义。"""

    id: NonEmptyString | None = None
    type: CapabilityType | None = None
    tags: frozenset[str] = Field(default_factory=frozenset)
    version: NonEmptyString | None = None
    plugin_id: NonEmptyString | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCapability:
    """能力描述及其可调用 Provider。"""

    descriptor: CapabilityDescriptor
    plugin_id: str
    provider: Capability
