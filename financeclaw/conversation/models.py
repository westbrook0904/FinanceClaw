"""Stable conversation, summary and model-context domain records."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CHILD = "waiting_child"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class SummaryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class Conversation(FrozenRecord):
    conversation_id: str
    tenant_id: str
    subject_id: str
    agent_id: str
    agent_profile_version: str
    agent_thread_id: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: datetime
    updated_at: datetime


class ConversationTurn(FrozenRecord):
    turn_id: str
    conversation_id: str
    tenant_id: str
    subject_id: str
    run_id: str
    server_run_id: str | None = None
    client_idempotency_key: str
    request_hash: str
    target_type: str
    target_id: str
    target_version: str
    status: TurnStatus
    created_at: datetime
    completed_at: datetime | None = None


class ConversationMessage(FrozenRecord):
    message_id: str
    conversation_id: str
    turn_id: str
    sequence: int = Field(ge=1)
    parent_message_id: str | None = None
    role: MessageRole
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible: bool = True
    created_at: datetime


class ConversationSummary(FrozenRecord):
    summary_id: str
    conversation_id: str
    level: int = Field(ge=0)
    start_sequence: int = Field(ge=1)
    end_sequence: int = Field(ge=1)
    source_message_ids: tuple[str, ...] = ()
    source_summary_ids: tuple[str, ...] = ()
    summary_content: str
    topics: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    model_profile_version: str
    template_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: SummaryStatus = SummaryStatus.ACTIVE
    superseded_by: str | None = None
    created_at: datetime


class ContextOmission(FrozenRecord):
    reason: Literal[
        "token_budget",
        "recent_window",
        "not_relevant",
        "artifact_offloaded",
        "current_input_truncated",
    ]
    item_type: Literal["message", "summary", "tool_result", "current_input"]
    item_id: str
    token_count: int = Field(ge=0)


class ManifestMemoryReference(FrozenRecord):
    """The versioned reason a memory was exposed to one model call."""

    memory_id: str
    schema_version: int = Field(ge=1)
    memory_type: Literal["preference", "goal", "constraint", "decision_note"]
    injection_reason: str


class ModelContextManifest(FrozenRecord):
    manifest_id: str
    model_call_id: str
    conversation_id: str
    turn_id: str
    run_id: str
    prompt_template_version: str
    agent_profile_version: str
    model_profile_version: str
    recent_message_start: int | None = None
    recent_message_end: int | None = None
    summary_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    memory_refs: tuple[ManifestMemoryReference, ...] = ()
    historical_message_ids: tuple[str, ...] = ()
    tool_result_refs: tuple[str, ...] = ()
    exposed_tools: tuple[str, ...] = ()
    input_token_count: int = Field(ge=0)
    available_input_tokens: int = Field(ge=1)
    omissions: tuple[ContextOmission, ...] = ()
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def memory_ids_must_match_versioned_references(self) -> Self:
        referenced_ids = tuple(item.memory_id for item in self.memory_refs)
        if self.memory_ids != referenced_ids:
            raise ValueError("manifest memory_ids must match memory_refs in order")
        if len(referenced_ids) != len(set(referenced_ids)):
            raise ValueError("manifest memory references must be unique")
        return self


class ContextSelection(FrozenRecord):
    """Serializable selection evidence; runtime messages stay as BaseMessage."""

    recent_message_ids: tuple[str, ...] = ()
    summary_ids: tuple[str, ...] = ()
    memory_refs: tuple[ManifestMemoryReference, ...] = ()
    historical_message_ids: tuple[str, ...] = ()
    tool_result_refs: tuple[str, ...] = ()
    input_token_count: int
    available_input_tokens: int
    omissions: tuple[ContextOmission, ...] = ()
    context_hash: str
    debug_payload: dict[str, Any] = Field(default_factory=dict)
