"""启动期装配的不可变工作流目录：登记固定版本并解析可用流程定义。

目录内容在进程启动时一次性装配，运行期只读，保证工作流、图修订、
ModelProfile 与工具版本可按版本精确复现。
"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from .models import WorkflowDefinition, WorkflowStatus


class WorkflowCatalogError(LookupError):
    """目录解析不到可用工作流时抛出的查找异常。

    使用场景：
        resolve 找不到指定版本或最新的 ACTIVE 工作流时抛出，
        调用方应转换为稳定错误响应。
    """

    pass


def _version_key(version: str) -> tuple[int, int, int]:
    """把语义版本字符串解析为可比较的整数元组，供目录选择最新版本。"""
    return tuple(int(part) for part in version.split("."))


class WorkflowCatalog(Mapping[tuple[str, str], WorkflowDefinition]):
    """按（workflow_id, version）索引的只读工作流目录。

    使用场景：
        启动期登记全部已发布流程；运行期按显式版本或最新 ACTIVE 版本
        解析定义，保证一次运行绑定确定的图、模型档案与工具集合。

    Attributes:
        _entries: 以（workflow_id, version）为键的只读定义映射。

    """

    def __init__(self, definitions: Iterable[WorkflowDefinition]) -> None:
        """装配目录并校验登记项。

        Args:
            definitions: 待登记的工作流定义集合。

        Raises:
            TypeError: 登记项不是 WorkflowDefinition。
            ValueError: 同一（workflow_id, version）被登记两次。

        """
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
        """按登记顺序迭代全部（workflow_id, version）键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录当前登记的条目数量。"""
        return len(self._entries)

    def resolve(self, workflow_id: str, version: str | None = None) -> WorkflowDefinition:
        """解析指定工作流的确定版本定义。

        Args:
            workflow_id: 工作流稳定标识。
            version: 期望版本；为空时解析该工作流最新的 ACTIVE 版本。

        Returns:
            命中的 ACTIVE 状态工作流定义。

        Raises:
            WorkflowCatalogError: 指定版本不存在，或不存在 ACTIVE 版本。

        """
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
        """返回目录中全部定义，按工作流标识与语义版本号升序排列。"""
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda item: (item.workflow_id, _version_key(item.version)),
            )
        )
