"""Deterministic request targets accepted by the BFF."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TargetId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
Version = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^\d+\.\d+\.\d+$")]


class ToolTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool"] = "tool"
    tool_id: TargetId
    version: Version | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class WorkflowTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workflow"] = "workflow"
    workflow_id: TargetId
    version: Version | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class AgentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"] = "agent"
    agent_id: TargetId
    version: Version | None = None


RunTarget = Annotated[ToolTarget | WorkflowTarget | AgentTarget, Field(discriminator="kind")]
