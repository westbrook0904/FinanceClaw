"""提供按语义版本登记和解析的受治理工具目录。"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from .governance import ManagedTool


class ToolCatalogError(LookupError):
    """定义工具CatalogError。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


def _version_key(version: str) -> tuple[int, int, int]:
    """把语义版本拆为整数元组，供目录选择最新版本。"""
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


class ToolCatalog(Mapping[tuple[str, str], ManagedTool]):
    """登记受治理工具版本并提供精确或最新版本查询。

    适用场景：
        用于运行时必须按显式版本复现配置，或选择最新兼容版本的场景。

    属性：
        _entries: 按复合键索引的只读目录内容。
    """

    def __init__(self, tools: Iterable[ManagedTool]) -> None:
        """注入并保存工具Catalog所需的协作对象，同时校验构造期不变量。"""
        entries: dict[tuple[str, str], ManagedTool] = {}
        for managed in tools:
            if not isinstance(managed, ManagedTool):
                raise TypeError("ToolCatalog entries must be ManagedTool values")
            if managed.key in entries:
                raise ValueError(f"duplicate tool version: {managed.key[0]}@{managed.key[1]}")
            entries[managed.key] = managed
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> ManagedTool:
        """按键读取目录项，保持 Mapping 接口语义。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """按目录内部的稳定顺序迭代所有键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录当前登记的条目数量。"""
        return len(self._entries)

    def resolve(self, tool_id: str, version: str | None = None) -> ManagedTool:
        """解析并校验工具Catalog，返回固定版本的运行对象。"""
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
        """按工具标识分组，返回每个工具当前登记的最高语义版本。"""
        ids = sorted({tool_id for tool_id, _ in self._entries})
        return tuple(self.resolve(tool_id) for tool_id in ids)
