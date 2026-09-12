"""有界工作状态和召回快照；全部字段由原生 LangGraph checkpoint 持久化。"""

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentState
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

WorkingStatement = Annotated[str, StringConstraints(max_length=1000)]


class WorkingContextDraft(BaseModel):
    """模型只能提议继续执行所需的内容，不能生成权限或来源身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(max_length=1500)
    scope: str = Field(default="", max_length=1000)
    constraints: tuple[WorkingStatement, ...] = Field(default=(), max_length=12)
    decisions: tuple[WorkingStatement, ...] = Field(default=(), max_length=12)
    completed_steps: tuple[WorkingStatement, ...] = Field(default=(), max_length=16)
    pending_questions: tuple[WorkingStatement, ...] = Field(default=(), max_length=12)
    next_steps: tuple[WorkingStatement, ...] = Field(default=(), max_length=12)


class WorkingContext(WorkingContextDraft):
    """唯一规范摘要正文；服务端绑定来源、版本、隐私状态和归档结果。"""

    evidence_refs: tuple[dict[str, Any], ...] = Field(default=(), max_length=64)
    summary_version: int = Field(ge=1)
    source_boundary: str = Field(min_length=1, max_length=128)
    privacy_epoch: int = Field(default=0, ge=0)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConversationState(AgentState):
    """每个 thread 的有界上下文投影；不另建 Journal 或摘要表。"""

    context_bootstrapped: NotRequired[bool]
    memory_recall: NotRequired[dict[str, Any]]
    memory_invalidated: NotRequired[bool]
    memory_forget_requested: NotRequired[bool]
    memory_privacy_epoch: NotRequired[int]
    context_privacy_epoch: NotRequired[int]
    working_context: NotRequired[dict[str, Any] | None]
    context_compaction_error: NotRequired[str | None]
    context_compaction_reason: NotRequired[str | None]
    context_compaction_fingerprint: NotRequired[str]
    context_compaction_attempts: NotRequired[int]
    summary_calls: NotRequired[int]
