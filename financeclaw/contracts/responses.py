"""Public API request, response, approval and stream contracts."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .targets import RunTarget


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(ContractModel):
    message: Annotated[str, Field(min_length=1, max_length=32_000)]
    target: RunTarget | None = None
    conversation_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class ToolInvokeRequest(ContractModel):
    version: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class RunAccepted(ContractModel):
    run_id: str
    thread_id: str
    status: str
    target_kind: str
    idempotent_replay: bool = False
    conversation_id: str | None = None
    turn_id: str | None = None


class CreateConversationRequest(ContractModel):
    agent_id: Annotated[str, Field(min_length=1, max_length=128)] = "finance_agent"
    agent_profile_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] | None = None


class ConversationResponse(ContractModel):
    conversation_id: str
    agent_id: str
    agent_profile_version: str
    status: str
    created_at: str


class ConversationMessageResponse(ContractModel):
    message_id: str
    turn_id: str
    sequence: int
    parent_message_id: str | None = None
    role: Literal["user", "assistant"]
    content: str
    created_at: str


class ConversationMessagesResponse(ContractModel):
    conversation_id: str
    messages: tuple[ConversationMessageResponse, ...]


class RunStatusResponse(ContractModel):
    run_id: str
    thread_id: str
    status: str
    output: dict[str, Any] | list[Any] | None = None


class AgentResponse(ContractModel):
    run_id: str
    thread_id: str
    status: str
    message: str | None = None


class ArtifactReference(ContractModel):
    artifact_id: str
    content_type: str
    content_hash: str
    size_bytes: int = Field(ge=0)


class DirectToolResponse(ContractModel):
    run_id: str
    tool_id: str
    tool_version: str
    status: Literal["success", "denied", "rejected", "failed", "interrupted"]
    result: Any = None
    error: str | None = None
    artifact: ArtifactReference | None = None
    arguments_hash: str | None = None


class ApprovalDecisionType(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class ApprovalDecision(ContractModel):
    type: ApprovalDecisionType
    arguments_hash: str | None = None
    arguments: dict[str, Any] | None = None
    reason: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> "ApprovalDecision":
        if self.type is ApprovalDecisionType.EDIT and self.arguments is None:
            raise ValueError("edit approval decision requires arguments")
        if self.type is not ApprovalDecisionType.EDIT and self.arguments is not None:
            raise ValueError("arguments are only valid for edit decisions")
        return self


class StreamEvent(ContractModel):
    event: str
    data: Any


class ErrorResponse(ContractModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
