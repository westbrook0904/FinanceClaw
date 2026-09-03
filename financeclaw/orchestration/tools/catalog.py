"""受治理 Tool 的目录：维护固定清单内各 Tool 的版本化条目并支持解析。

属于 orchestration/tools 治理层，上游（如 API 直连调用、MCP Server 装配）
通过目录把 tool_id+version 解析为具体的 ManagedTool。
"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from .governance import ManagedTool


class ToolCatalogError(LookupError):
    """在目录中找不到请求的 Tool 或版本时抛出的查询异常。

    使用场景：resolve 未命中 tool_id 或指定 version 时抛出，调用方应把
    其映射为稳定的"未知工具"错误响应，而不是让查询静默失败。
    """

    pass


def _version_key(version: str) -> tuple[int, int, int]:
    """把语义化版本字符串解析为可比较的整数三元组。

    Args:
        version: 形如 ``major.minor.patch`` 的版本字符串。

    Returns:
        用于排序比较的 (major, minor, patch) 整数元组。

    """
    return tuple(int(part) for part in version.split("."))


class ToolCatalog(Mapping[tuple[str, str], ManagedTool]):
    """受治理 Tool 的只读目录，按 (tool_id, version) 索引各 ManagedTool。

    使用场景：装配阶段把全部 ManagedTool 注册进目录；运行期由 API 直连
    调用、MCP Server 等消费方按 tool_id+version 精确解析，或按 tool_id
    取最新版本。目录本身不可变，保证运行期清单稳定可审计。

    Attributes:
        _entries: (tool_id, version) 到 ManagedTool 的不可变映射，
            用 ``MappingProxyType`` 包装防止运行期被篡改。

    """

    def __init__(self, tools: Iterable[ManagedTool]) -> None:
        """用给定的受治理 Tool 构建只读目录。

        Args:
            tools: 待纳入目录的 ManagedTool 集合，允许重复 tool_id
                但不允许重复 (tool_id, version) 组合。

        Raises:
            TypeError: 条目不是 ManagedTool 实例。
            ValueError: 出现重复的 tool_id+version 组合。

        """
        # 1. 逐个校验条目类型并按 (tool_id, version) 建立索引。
        entries: dict[tuple[str, str], ManagedTool] = {}
        for managed in tools:
            if not isinstance(managed, ManagedTool):
                raise TypeError("ToolCatalog entries must be ManagedTool values")
            if managed.key in entries:
                raise ValueError(f"duplicate tool version: {managed.key[0]}@{managed.key[1]}")
            entries[managed.key] = managed
        # 2. 用只读代理包装索引，保证目录构建后不可变。
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> ManagedTool:
        """按 (tool_id, version) 键取出受治理 Tool；键不存在时抛 KeyError。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """迭代目录中的全部 (tool_id, version) 键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录中受治理 Tool 版本条目的总数。"""
        return len(self._entries)

    def resolve(self, tool_id: str, version: str | None = None) -> ManagedTool:
        """按 tool_id 解析 Tool；指定 version 时精确匹配，否则取最新版本。

        Args:
            tool_id: Tool 标识，必须与治理元数据中的 tool_id 一致。
            version: 可选的语义化版本号；为 None 时解析该 tool_id 的最新版本。

        Returns:
            对应的 ManagedTool 条目。

        Raises:
            ToolCatalogError: tool_id 未知，或指定的 version 不存在。

        """
        # 1. 显式给出版本时按 (tool_id, version) 精确查找。
        if version is not None:
            try:
                return self._entries[(tool_id, version)]
            except KeyError as exc:
                raise ToolCatalogError(f"unknown tool version: {tool_id}@{version}") from exc
        # 2. 未给出版本时收集同 tool_id 的全部候选，按语义化版本取最新。
        candidates = [
            managed
            for (candidate_id, _), managed in self._entries.items()
            if candidate_id == tool_id
        ]
        if not candidates:
            raise ToolCatalogError(f"unknown tool: {tool_id}")
        return max(candidates, key=lambda item: _version_key(item.governance.version))

    def latest(self) -> tuple[ManagedTool, ...]:
        """返回目录中每个 tool_id 的最新版本条目，按 tool_id 排序。

        Returns:
            以 (tool_id, 最新版本) 组织的 ManagedTool 元组，顺序确定可复现。

        """
        # 1. 先收集去重后的全部 tool_id 并排序，保证输出顺序稳定。
        ids = sorted({tool_id for tool_id, _ in self._entries})
        # 2. 再逐个解析到最新版本。
        return tuple(self.resolve(tool_id) for tool_id in ids)
