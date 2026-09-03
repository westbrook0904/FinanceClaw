"""登记并解析不可变版本的确定性工作流定义。"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from .models import WorkflowDefinition, WorkflowStatus


class WorkflowCatalogError(LookupError):
    """定义工作流CatalogError。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


def _version_key(version: str) -> tuple[int, int, int]:
    """把语义版本拆为整数元组，供目录选择最新版本。"""
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


class WorkflowCatalog(Mapping[tuple[str, str], WorkflowDefinition]):
    """登记工作流版本并解析显式版本或某工作流的最新版本。

    适用场景：
        用于运行时必须按显式版本复现配置，或选择最新兼容版本的场景。

    属性：
        _entries: 按复合键索引的只读目录内容。
    """

    def __init__(self, definitions: Iterable[WorkflowDefinition]) -> None:
        """注入并保存工作流Catalog所需的协作对象，同时校验构造期不变量。"""
        entries: dict[tuple[str, str], WorkflowDefinition] = {}
        for definition in definitions:
            if not isinstance(definition, WorkflowDefinition):
                raise TypeError("WorkflowCatalog entries must be WorkflowDefinition values")
            if definition.key in entries:
                raise ValueError(
                    f"duplicate workflow version: {definition.workflow_id}@{definition.version}"
                )
            entries[definition.key] = definition
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> WorkflowDefinition:
        """按键读取目录项，保持 Mapping 接口语义。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """按目录内部的稳定顺序迭代所有键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录当前登记的条目数量。"""
        return len(self._entries)

    def resolve(self, workflow_id: str, version: str | None = None) -> WorkflowDefinition:
        """解析并校验工作流Catalog，返回固定版本的运行对象。"""
        if version is not None:
            candidate = self._entries.get((workflow_id, version))
            if candidate is None or candidate.status is not WorkflowStatus.ACTIVE:
                raise WorkflowCatalogError(f"unknown active workflow: {workflow_id}@{version}")
            return candidate
        candidates = [
            definition
            for (candidate_id, _), definition in self._entries.items()
            if candidate_id == workflow_id and definition.status is WorkflowStatus.ACTIVE
        ]
        if not candidates:
            raise WorkflowCatalogError(f"unknown active workflow: {workflow_id}")
        return max(candidates, key=lambda item: _version_key(item.version))

    def published(self) -> tuple[WorkflowDefinition, ...]:
        """返回所有已发布、可被新运行解析的工作流定义。"""
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda item: (item.workflow_id, _version_key(item.version)),
            )
        )
