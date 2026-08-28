"""SPI 自有的调用参数与插件清单模型。"""

from __future__ import annotations

from harness_contracts import CapabilityDescriptor, ContractModel, RequestInput
from harness_contracts.base import FrozenJsonMapping, NonEmptyString
from pydantic import Field, field_validator


class AgentRequest(ContractModel):
    """交给 Agent 的任务。

    ``input`` 保留调用方声明的输入类型；``instructions`` 用于 Runtime 或未来
    Planner 给 Agent 补充任务约束，而不污染原始 Request。
    """

    input: RequestInput
    instructions: NonEmptyString | None = None


class ToolRequest(ContractModel):
    """交给 Tool 的确定性调用参数。"""

    arguments: FrozenJsonMapping = Field(default_factory=dict)


class PluginManifest(ContractModel):
    """阶段一本地插件的稳定身份和能力声明。

    ``capabilities`` 只保存能力 ID；能力的完整 Schema 由对应 Provider 的
    :meth:`descriptor` 返回，避免 Manifest 与 Descriptor 出现两份真相。
    """

    plugin_id: NonEmptyString
    name: NonEmptyString
    version: NonEmptyString
    sdk_version: NonEmptyString
    capabilities: tuple[NonEmptyString, ...]
    metadata: FrozenJsonMapping = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("plugin must declare at least one capability")
        if len(value) != len(set(value)):
            raise ValueError("plugin capability ids must be unique")
        return value


def validate_manifest_capabilities(
    manifest: PluginManifest,
    descriptors: tuple[CapabilityDescriptor, ...],
) -> None:
    """校验清单声明与 Provider 描述完全一致。

    Loader 在注册前调用此函数，能够尽早发现漏报、多报和重复 Provider。
    """

    descriptor_ids = tuple(descriptor.id for descriptor in descriptors)
    if len(descriptor_ids) != len(set(descriptor_ids)):
        raise ValueError("plugin providers must have unique capability ids")
    if set(manifest.capabilities) != set(descriptor_ids):
        raise ValueError("manifest capabilities do not match plugin providers")
