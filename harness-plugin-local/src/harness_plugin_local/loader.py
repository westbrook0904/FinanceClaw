"""本地插件生命周期协调器。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from harness_contracts import CapabilityDescriptor, CapabilityType, PluginError
from harness_registry import CapabilityRegistry
from harness_spi import (
    AgentSPI,
    Capability,
    PluginManifest,
    PluginSPI,
    ToolSPI,
    validate_manifest_capabilities,
)

from .provider import LocalPluginProvider


class PluginState(StrEnum):
    DISCOVERED = "discovered"
    INITIALIZED = "initialized"
    REGISTERED = "registered"
    ACTIVE = "active"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """Loader 中已激活插件的只读快照。"""

    manifest: PluginManifest
    plugin: PluginSPI
    capability_ids: tuple[str, ...]
    state: PluginState = PluginState.ACTIVE


class LocalPluginLoader:
    """发现本地插件，并以单插件事务执行初始化和注册。"""

    def __init__(
        self,
        registry: CapabilityRegistry,
        provider: LocalPluginProvider | None = None,
    ) -> None:
        self._registry = registry
        self._provider = provider or LocalPluginProvider()
        self._loaded: dict[str, LoadedPlugin] = {}
        self._lock = asyncio.Lock()

    def discover(self) -> tuple[PluginSPI, ...]:
        """从配置的本地来源发现插件，但不产生生命周期副作用。"""

        return self._provider.discover()

    def loaded_plugins(self) -> tuple[LoadedPlugin, ...]:
        """返回按 plugin_id 排序的当前活动插件快照。"""

        return tuple(self._loaded[key] for key in sorted(self._loaded))

    async def load(self, plugin: PluginSPI) -> LoadedPlugin:
        """校验、初始化并注册一个插件；任一步失败都会回滚。"""

        async with self._lock:
            return await self._load_locked(plugin)

    async def load_all(self) -> tuple[LoadedPlugin, ...]:
        """发现并加载全部插件；批次失败时卸载本批次已经加载的插件。"""

        plugins = self.discover()
        loaded_in_batch: list[LoadedPlugin] = []
        async with self._lock:
            try:
                for plugin in plugins:
                    loaded_in_batch.append(await self._load_locked(plugin))
            except BaseException:
                for record in reversed(loaded_in_batch):
                    await self._unload_locked(record.manifest.plugin_id, suppress_errors=True)
                raise
        return tuple(loaded_in_batch)

    async def unload(self, plugin_id: str) -> LoadedPlugin:
        """先从 Registry 摘除能力，再关闭插件。"""

        async with self._lock:
            return await self._unload_locked(plugin_id)

    async def shutdown(self) -> None:
        """以加载逆序关闭全部插件，并尽可能完成所有清理。"""

        errors: list[str] = []
        async with self._lock:
            for plugin_id in reversed(tuple(self._loaded)):
                try:
                    await self._unload_locked(plugin_id)
                except PluginError as exc:
                    errors.append(exc.message)
        if errors:
            raise PluginError(
                "one or more local plugins failed to shut down",
                details={"errors": errors},
            )

    async def _load_locked(self, plugin: PluginSPI) -> LoadedPlugin:
        if not isinstance(plugin, PluginSPI):
            raise PluginError("local plugin must implement PluginSPI")

        try:
            manifest = plugin.manifest()
            providers = tuple(plugin.capabilities())
            descriptors = tuple(provider.descriptor() for provider in providers)
            validate_manifest_capabilities(manifest, descriptors)
            _validate_provider_types(providers, descriptors)
        except Exception as exc:
            raise _as_plugin_error("local plugin validation failed", exc) from exc

        if manifest.plugin_id in self._loaded:
            raise PluginError(
                f"plugin already loaded: {manifest.plugin_id}",
                details={"plugin_id": manifest.plugin_id},
            )

        initialized = False
        registered_ids: list[str] = []
        try:
            await plugin.initialize()
            initialized = True
            for provider, descriptor in zip(providers, descriptors, strict=True):
                self._registry.register(provider, plugin_id=manifest.plugin_id)
                registered_ids.append(descriptor.id)
        except BaseException as exc:
            for capability_id in reversed(registered_ids):
                try:
                    self._registry.unregister(capability_id, plugin_id=manifest.plugin_id)
                except Exception:
                    pass
            if initialized:
                try:
                    await plugin.shutdown()
                except Exception:
                    pass
            if isinstance(exc, Exception):
                raise _as_plugin_error(
                    f"failed to load local plugin: {manifest.plugin_id}", exc
                ) from exc
            raise

        record = LoadedPlugin(
            manifest=manifest,
            plugin=plugin,
            capability_ids=tuple(registered_ids),
        )
        self._loaded[manifest.plugin_id] = record
        return record

    async def _unload_locked(
        self,
        plugin_id: str,
        *,
        suppress_errors: bool = False,
    ) -> LoadedPlugin:
        record = self._loaded.get(plugin_id)
        if record is None:
            raise PluginError(
                f"plugin is not loaded: {plugin_id}",
                details={"plugin_id": plugin_id},
            )

        errors: list[str] = []
        for capability_id in reversed(record.capability_ids):
            try:
                self._registry.unregister(capability_id, plugin_id=plugin_id)
            except Exception as exc:
                errors.append(str(exc))
        try:
            await record.plugin.shutdown()
        except Exception as exc:
            errors.append(str(exc))

        self._loaded.pop(plugin_id, None)
        stopped = LoadedPlugin(
            manifest=record.manifest,
            plugin=record.plugin,
            capability_ids=record.capability_ids,
            state=PluginState.STOPPED,
        )
        if errors and not suppress_errors:
            raise PluginError(
                f"failed to cleanly unload plugin: {plugin_id}",
                details={"plugin_id": plugin_id, "errors": errors},
            )
        return stopped


def _validate_provider_types(
    providers: tuple[Capability, ...],
    descriptors: tuple[CapabilityDescriptor, ...],
) -> None:
    for provider, descriptor in zip(providers, descriptors, strict=True):
        is_agent = isinstance(provider, AgentSPI)
        is_tool = isinstance(provider, ToolSPI)
        if is_agent == is_tool:
            raise ValueError("provider must implement exactly one of AgentSPI or ToolSPI")
        expected = CapabilityType.AGENT if is_agent else CapabilityType.TOOL
        if descriptor.type is not expected:
            raise ValueError(
                f"provider type does not match descriptor for capability: {descriptor.id}"
            )


def _as_plugin_error(message: str, cause: Exception) -> PluginError:
    if isinstance(cause, PluginError):
        return cause
    return PluginError(message, details={"cause": str(cause)})
