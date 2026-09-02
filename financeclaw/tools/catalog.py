"""Immutable startup-time ToolCatalog."""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from .governance import ManagedTool


class ToolCatalogError(LookupError):
    pass


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


class ToolCatalog(Mapping[tuple[str, str], ManagedTool]):
    """Read-only mapping; updates require constructing and deploying a new catalog."""

    def __init__(self, tools: Iterable[ManagedTool]) -> None:
        entries: dict[tuple[str, str], ManagedTool] = {}
        for managed in tools:
            if not isinstance(managed, ManagedTool):
                raise TypeError("ToolCatalog entries must be ManagedTool values")
            if managed.key in entries:
                raise ValueError(f"duplicate tool version: {managed.key[0]}@{managed.key[1]}")
            entries[managed.key] = managed
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> ManagedTool:
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, tool_id: str, version: str | None = None) -> ManagedTool:
        if version is not None:
            try:
                return self._entries[(tool_id, version)]
            except KeyError as exc:
                raise ToolCatalogError(f"unknown tool version: {tool_id}@{version}") from exc
        candidates = [
            managed
            for (candidate_id, _), managed in self._entries.items()
            if candidate_id == tool_id
        ]
        if not candidates:
            raise ToolCatalogError(f"unknown tool: {tool_id}")
        return max(candidates, key=lambda item: _version_key(item.governance.version))

    def latest(self) -> tuple[ManagedTool, ...]:
        ids = sorted({tool_id for tool_id, _ in self._entries})
        return tuple(self.resolve(tool_id) for tool_id in ids)
