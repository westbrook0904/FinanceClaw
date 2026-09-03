"""LangChain tools exposing the governed memory use cases to an Agent."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from financeclaw.contracts import DataClassification, ExecutionContext
from financeclaw.tools.governance import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    ManagedTool,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)

from .models import MemoryDraft, MemoryType
from .policy import MemoryPolicyViolation
from .service import LongTermMemoryService, MemoryServiceError


class MemoryToolInput(BaseModel):
    """Common strict parsing policy for every model-visible memory tool."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # ToolNode supplies this hidden value from the trusted graph runtime. It is
    # declared on the full input schema so BaseTool subclasses participate in
    # ToolRuntime injection, while LangChain omits it from the model schema.
    runtime: ToolRuntime[ExecutionContext]


class SearchMemoriesInput(MemoryToolInput):
    query: str | None = Field(default=None, max_length=512)
    kinds: tuple[MemoryType, ...] | None = None
    limit: int = Field(default=5, ge=1, le=20)


class ProposeMemoryInput(MemoryToolInput):
    kind: MemoryType
    content: str = Field(min_length=1, max_length=2_000)
    evidence_message_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class ConfirmMemoryInput(ProposeMemoryInput):
    proposal_id: str = Field(min_length=1, max_length=128)
    supersedes_id: str | None = Field(default=None, max_length=128)


class ForgetMemoryInput(MemoryToolInput):
    memory_id: str = Field(min_length=1, max_length=128)
    mode: Literal["revoke", "delete"] = "revoke"


class _MemoryTool(BaseTool):
    """Shared service wiring and safe error mapping for concrete BaseTools."""

    handle_tool_error: bool = True
    _service: LongTermMemoryService = PrivateAttr()

    def __init__(self, service: LongTermMemoryService, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service

    @staticmethod
    def _context(runtime: ToolRuntime[ExecutionContext]) -> ExecutionContext:
        value = runtime.context
        if isinstance(value, ExecutionContext):
            return value
        return ExecutionContext.model_validate(value)

    @staticmethod
    def _draft(
        *, kind: MemoryType, content: str, evidence_message_ids: tuple[str, ...]
    ) -> MemoryDraft:
        return MemoryDraft(
            kind=kind,
            content=content,
            evidence_message_ids=evidence_message_ids,
        )

    @staticmethod
    def _failure(error: Exception) -> ToolException:
        if isinstance(error, MemoryServiceError | MemoryPolicyViolation):
            reason = error.reason
        else:
            reason = "memory_operation_failed"
        return ToolException(
            json.dumps(
                {"status": "rejected", "reason": reason, "message": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )


class SearchMemoriesTool(_MemoryTool):
    name: str = "search_memories"
    description: str = (
        "Search active, user-approved long-term memories for the authenticated user. "
        "Use current-data tools instead for prices, holdings, balances or other financial facts."
    )
    args_schema: type[BaseModel] = SearchMemoriesInput

    def _run(
        self,
        query: str | None = None,
        kinds: tuple[MemoryType, ...] | None = None,
        limit: int = 5,
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        try:
            recalls = self._service.search(
                self._context(runtime),
                runtime.store,
                query=query,
                kinds=kinds,
                limit=limit,
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {
                "memories": [
                    {
                        "memory_id": item.record.memory_id,
                        "memory_type": item.record.memory_type.value,
                        "content": item.record.content,
                        "schema_version": item.record.schema_version,
                        "reason": item.reason,
                    }
                    for item in recalls
                ]
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class ProposeMemoryTool(_MemoryTool):
    name: str = "propose_memory"
    description: str = (
        "Validate a possible stable preference, goal, constraint or confirmed decision. "
        "This does not persist memory. Pass evidence_message_ids=['current'] to bind the current "
        "user Journal message; never propose prices, holdings, balances, credentials or news."
    )
    args_schema: type[BaseModel] = ProposeMemoryInput

    def _run(
        self,
        kind: MemoryType,
        content: str,
        evidence_message_ids: tuple[str, ...],
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        try:
            proposal = self._service.propose(
                self._context(runtime),
                self._draft(
                    kind=kind,
                    content=content,
                    evidence_message_ids=evidence_message_ids,
                ),
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return proposal.model_dump_json()


class ConfirmMemoryTool(_MemoryTool):
    name: str = "confirm_memory"
    description: str = (
        "Persist the exact output of propose_memory after explicit user approval. "
        "The proposal ID, content and resolved evidence must remain unchanged."
    )
    args_schema: type[BaseModel] = ConfirmMemoryInput

    def _run(
        self,
        kind: MemoryType,
        content: str,
        evidence_message_ids: tuple[str, ...],
        proposal_id: str,
        supersedes_id: str | None = None,
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        try:
            record = self._service.confirm(
                self._context(runtime),
                runtime.store,
                proposal_id=proposal_id,
                draft=self._draft(
                    kind=kind,
                    content=content,
                    evidence_message_ids=evidence_message_ids,
                ),
                # This tool is always guarded by HumanInTheLoopMiddleware. A
                # model cannot set or override this authoritative signal.
                user_confirmed=True,
                supersedes_id=supersedes_id,
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {
                "status": "committed",
                "memory_id": record.memory_id,
                "memory_type": record.memory_type.value,
                "schema_version": record.schema_version,
            },
            sort_keys=True,
        )


class ForgetMemoryTool(_MemoryTool):
    name: str = "forget_memory"
    description: str = (
        "Revoke or logically delete one long-term memory owned by the authenticated user."
    )
    args_schema: type[BaseModel] = ForgetMemoryInput

    def _run(
        self,
        memory_id: str,
        mode: Literal["revoke", "delete"] = "revoke",
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> str:
        try:
            record = self._service.forget(
                self._context(runtime), runtime.store, memory_id, mode=mode
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {"status": record.status.value, "memory_id": record.memory_id},
            sort_keys=True,
        )


def default_memory_tools(service: LongTermMemoryService) -> tuple[ManagedTool, ...]:
    """Return the immutable catalog entries for the Stage-3 memory surface."""

    readable_classes = frozenset({DataClassification.INTERNAL, DataClassification.CONFIDENTIAL})
    internal = dict(
        version="1.0.0",
        direct_invocation=False,
        egress=Egress.INTERNAL,
        sensitivity=Sensitivity.CONFIDENTIAL,
        retry_profile=RetryProfile.NONE,
        audit_level=AuditLevel.FULL,
        allowed_data_classes=readable_classes,
    )
    return (
        ManagedTool(
            SearchMemoriesTool(service),
            ToolGovernance(
                tool_id="search_memories",
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.LOW,
                required_scopes=frozenset({"memory:read"}),
                approval=ApprovalMode.NONE,
                **internal,
            ),
        ),
        ManagedTool(
            ProposeMemoryTool(service),
            ToolGovernance(
                tool_id="propose_memory",
                # Proposal validation does not modify long-term memory. The
                # durable mutation is isolated in confirm_memory below.
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.LOW,
                required_scopes=frozenset({"memory:write"}),
                approval=ApprovalMode.NONE,
                **internal,
            ),
        ),
        ManagedTool(
            ConfirmMemoryTool(service),
            ToolGovernance(
                tool_id="confirm_memory",
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.KEY_REQUIRED,
                risk_level=RiskLevel.MEDIUM,
                required_scopes=frozenset({"memory:write"}),
                approval=ApprovalMode.ALWAYS,
                **internal,
            ),
        ),
        ManagedTool(
            ForgetMemoryTool(service),
            ToolGovernance(
                tool_id="forget_memory",
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.IDEMPOTENT,
                risk_level=RiskLevel.MEDIUM,
                required_scopes=frozenset({"memory:delete"}),
                approval=ApprovalMode.ALWAYS,
                **internal,
            ),
        ),
    )
