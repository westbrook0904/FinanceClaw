"""本地 Plugin 的发现来源。"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import Any

from harness_contracts import PluginError
from harness_spi import PluginSPI


class LocalPluginProvider:
    """发现显式提供或通过 Python entry point 发布的本地插件。

    Entry point 默认组名为 ``financeclaw.plugins``，目标可以是 PluginSPI 实例、
    PluginSPI 子类或返回 PluginSPI 的无参工厂。
    """

    def __init__(
        self,
        plugins: Iterable[PluginSPI] = (),
        *,
        entry_point_group: str | None = "financeclaw.plugins",
    ) -> None:
        self._plugins = tuple(plugins)
        self._entry_point_group = entry_point_group

    def discover(self) -> tuple[PluginSPI, ...]:
        discovered = list(self._plugins)
        if self._entry_point_group is not None:
            for entry_point in entry_points(group=self._entry_point_group):
                try:
                    discovered.append(_materialize(entry_point.load()))
                except Exception as exc:
                    raise PluginError(
                        f"failed to discover local plugin: {entry_point.name}",
                        details={"entry_point": entry_point.name},
                    ) from exc

        plugin_ids: set[str] = set()
        for plugin in discovered:
            if not isinstance(plugin, PluginSPI):
                raise PluginError("discovered object must implement PluginSPI")
            plugin_id = plugin.manifest().plugin_id
            if plugin_id in plugin_ids:
                raise PluginError(
                    f"duplicate discovered plugin id: {plugin_id}",
                    details={"plugin_id": plugin_id},
                )
            plugin_ids.add(plugin_id)
        return tuple(discovered)


def _materialize(candidate: Any) -> PluginSPI:
    if isinstance(candidate, PluginSPI):
        return candidate
    if callable(candidate):
        candidate = candidate()
    if not isinstance(candidate, PluginSPI):
        raise TypeError("entry point must produce PluginSPI")
    return candidate
