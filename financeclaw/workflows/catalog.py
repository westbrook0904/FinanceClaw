"""Immutable startup-time catalog for code-published workflows."""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from .models import WorkflowDefinition, WorkflowStatus


class WorkflowCatalogError(LookupError):
    pass


def _version_key(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


class WorkflowCatalog(Mapping[tuple[str, str], WorkflowDefinition]):
    """Read-only release map; updates require a new application deployment."""

    def __init__(self, definitions: Iterable[WorkflowDefinition]) -> None:
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
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, workflow_id: str, version: str | None = None) -> WorkflowDefinition:
        """Resolve only versions accepting new traffic; active runs use their stored pin."""

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
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda item: (item.workflow_id, _version_key(item.version)),
            )
        )
