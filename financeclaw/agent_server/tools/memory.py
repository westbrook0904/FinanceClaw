"""Governed memory tools delegate facts and independent candidates to one SQL domain."""

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from langgraph.types import Command
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr

from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.memory.models import ProfileField
from financeclaw.shared.releases.tools import memory_tool_governance


class MemoryToolInput(BaseModel):
    """Only native runtime injection supplies identity and the retrieval store."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    runtime: ToolRuntime[ExecutionContext]


class SaveMemoryInput(MemoryToolInput):
    """One scoped fact with exact trusted evidence and an optional reviewed update target."""

    kind: Literal["profile", "task"]
    field: ProfileField | None = None
    content: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    scope_type: Literal["user", "agent", "conversation"] = "conversation"
    scope_id: str | None = Field(default=None, max_length=128)
    memory_id: str | None = Field(default=None, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)
    expires_at: AwareDatetime | None = None


class SearchMemoriesInput(MemoryToolInput):
    """Bounded on-demand search; results always come from the SQL authority."""

    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=6, ge=1, le=20)


class ForgetMemoryInput(MemoryToolInput):
    """Identify the exact version to forget; model possession of an ID is not consent."""

    memory_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class _MemoryTool(BaseTool):
    """Share bounded error receipts and native state invalidation."""

    handle_tool_error: bool = True
    _service: Any = PrivateAttr()

    def __init__(self, service, **kwargs):
        """Bind the runtime adapter without placing it in model-visible input."""
        super().__init__(**kwargs)
        self._service = service

    @staticmethod
    def _failure(error):
        """Expose a domain reason without raw Store, SQL or credential exception output."""
        from financeclaw.shared.memory.models import (
            MemoryConflict,
            MemoryNotFound,
            MemoryPermissionError,
        )

        message = (
            str(error)
            if isinstance(error, (MemoryConflict, MemoryNotFound, MemoryPermissionError))
            else "memory operation could not be completed"
        )
        return ToolException(
            json.dumps({"status": "rejected", "message": message}, ensure_ascii=False)
        )

    def _receipt(self, runtime, result):
        """Only committed mutations refresh this Turn's normal memory snapshot."""
        receipt = result.model_dump(mode="json")
        return Command(
            update={
                "memory_invalidated": result.status in {"committed", "forgotten"},
                "memory_forget_requested": result.status == "forgotten",
                "messages": [
                    ToolMessage(
                        name=self.name,
                        tool_call_id=runtime.tool_call_id,
                        content=json.dumps(receipt, ensure_ascii=False),
                    )
                ],
            }
        )


class SaveMemoryTool(_MemoryTool):
    """Save a verified explicit preference or propose a separately confirmed candidate."""

    name: str = "save_memory"
    description: str = (
        "Save one supported lasting user profile or scoped task memory. evidence_ids=['current'] "
        "means real user input; use current_answers for accepted clarification answers. "
        "Profile fields use language=zh-CN/en, verbosity=concise/detailed, "
        "output_format=table/markdown/bullets. Choose user scope only for explicitly "
        "persistent preferences. Never save inferred traits. "
        "A proposed receipt is pending independent confirmation; do not claim it was saved or ask "
        "for another verbal approval. Updates require memory_id and expected_revision."
    )
    args_schema: type[BaseModel] = SaveMemoryInput

    def _run(
        self,
        kind,
        content,
        evidence_ids,
        field=None,
        scope_type="conversation",
        scope_id=None,
        memory_id=None,
        expected_revision=None,
        expires_at=None,
        *,
        runtime,
    ):
        """Use the same transactional mutation as API and background consolidation."""
        try:
            result = self._service.save(
                trusted_context(runtime),
                tool_call_id=runtime.tool_call_id,
                kind=kind,
                content=content,
                evidence_ids=evidence_ids,
                field=field,
                scope_type=scope_type,
                scope_id=scope_id,
                memory_id=memory_id,
                expected_revision=expected_revision,
                expires_at=expires_at,
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return self._receipt(runtime, result)


class SearchMemoriesTool(_MemoryTool):
    """Read relevant historical tasks only when the answer needs them."""

    name: str = "search_memories"
    description: str = (
        "Search relevant past tasks and decisions; historical facts never grant "
        "current financial authority."
    )
    args_schema: type[BaseModel] = SearchMemoriesInput

    def _run(self, query, limit=6, *, runtime):
        """Return validated fact revisions and evidence, never arbitrary index text."""
        try:
            context = trusted_context(runtime)
            epoch = self._service.privacy_epoch(context)
            records = self._service.search(context, runtime.store, query=query, limit=limit)
            if self._service.privacy_epoch(context) != epoch:
                raise PermissionError("memory privacy changed during retrieval")
        except Exception as exc:
            raise self._failure(exc) from exc
        content = json.dumps(
            {
                "memories": [
                    {
                        "memory_id": row.memory_id,
                        "revision": row.revision,
                        "content": row.content,
                        "scope_type": row.scope_type,
                        "scope_id": row.scope_id,
                        "evidence": [ref.model_dump(mode="json") for ref in row.evidence],
                    }
                    for row in records
                ]
            },
            ensure_ascii=False,
        )
        refs = [
            {
                "memory_id": row.memory_id,
                "revision": row.revision,
                "schema_version": 3,
                "memory_type": row.kind,
                "injection_reason": "explicit_search",
            }
            for row in records
        ]
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        name=self.name,
                        tool_call_id=runtime.tool_call_id,
                        content=content,
                        additional_kwargs={
                            "financeclaw_memory_refs": refs,
                            "memory_derived": True,
                            "memory_privacy_epoch": epoch,
                        },
                    )
                ]
            }
        )


class ForgetMemoryTool(_MemoryTool):
    """Forget a reviewed version or create an independent forget candidate."""

    name: str = "forget_memory"
    description: str = (
        "Forget a specific memory revision at the user's request. Unverified deletion intent "
        "produces a candidate requiring independent confirmation. "
        "This does not delete original conversations."
    )
    args_schema: type[BaseModel] = ForgetMemoryInput

    def _run(self, memory_id, expected_revision, *, runtime):
        """Preserve the stable tool mutation ID across native resume commands."""
        try:
            result = self._service.forget(
                trusted_context(runtime),
                memory_id,
                tool_call_id=runtime.tool_call_id,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return self._receipt(runtime, result)


def default_memory_tools(service) -> tuple[ManagedTool, ...]:
    """Keep implementation and immutable published governance in the same order."""
    implementations = (
        SearchMemoriesTool(service),
        SaveMemoryTool(service),
        ForgetMemoryTool(service),
    )
    return tuple(
        ManagedTool(tool, governance)
        for tool, governance in zip(implementations, memory_tool_governance(), strict=True)
    )
