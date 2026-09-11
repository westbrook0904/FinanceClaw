"""根 Agent 的原生状态扩展；所有字段均随 LangGraph checkpoint 持久化。"""

from typing import Any, NotRequired

from langchain.agents.middleware import AgentState


class ConversationState(AgentState):
    """有界的初始化标志、工作摘要来源和当前 Turn 召回结果。"""

    context_bootstrapped: NotRequired[bool]
    memory_recall: NotRequired[dict[str, Any]]
    memory_invalidated: NotRequired[bool]
    memory_forget_requested: NotRequired[bool]
    context_compaction_error: NotRequired[str]
    summary_calls: NotRequired[int]
