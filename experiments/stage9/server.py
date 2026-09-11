"""真实 Agent Server：框架中间件与业务服务，模型仅返回可断言的合成消息。"""

import json
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from financeclaw.agent_server.context.budget import ContextBudget
from financeclaw.agent_server.context.compaction import NativeContextMiddleware
from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.agent_server.memory.embeddings import ConfiguredEmbeddings
from financeclaw.agent_server.memory.service import LongTermMemoryService
from financeclaw.agent_server.middleware.artifact_middleware import ToolResultArtifactMiddleware
from financeclaw.agent_server.middleware.context_editing import ToolContextEditingMiddleware
from financeclaw.agent_server.middleware.final_context import (
    FinalContextMiddleware,
    RequestRecorder,
)
from financeclaw.agent_server.middleware.memory_middleware import MemoryRecallMiddleware
from financeclaw.agent_server.tools.memory import SaveMemoryTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.turns import is_user_message
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


class ProbeEmbeddings(ConfiguredEmbeddings):
    """追加合成调用计数，明确不验证真实语义召回质量。"""

    def _measure(self, kind, texts, start, *, success):
        """记录调用发生的类型与条数，不持久化被编码正文。"""
        super()._measure(kind, texts, start, success=success)
        with Path("embedding.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": kind, "count": len(texts), "success": success}) + "\n")


embeddings = ProbeEmbeddings()


@tool
def external_mcp_snapshot() -> str:
    """Return synthetic detail with no retention or reread annotation."""
    return "明细原文：数值为 1729。" + "不可重新执行来替代旧快照。" * 400


class ScriptModel(BaseChatModel):
    """只检查上下文与工具流程，不访问聊天模型供应商。"""

    @property
    def _llm_type(self):
        """区别于生产模型的探针标识。"""
        return "stage9-native-probe"

    def bind_tools(self, tools, **kwargs):
        """接受真实工具 schema，但使用确定性脚本产生调用。"""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """根据当前 Turn 的标记选择一次工具动作或返回来源计数。"""
        if len(messages) == 1 and not is_user_message(messages[0]):
            answer = AIMessage(content="历史摘要：已查询明细，后续需要通过工件引用回读。")
        elif "<conversation>" in str(messages[0].content):
            answer = AIMessage(content="历史摘要：已查询明细，保留来源以供回读。")
        elif isinstance(messages[-1], ToolMessage):
            answer = AIMessage(content=f"完成：{messages[-1].content}")
        else:
            user = next(item for item in reversed(messages) if is_user_message(item))
            text = str(user.content)
            if text.startswith("查明细"):
                call = {"name": "external_mcp_snapshot", "args": {}, "id": f"detail-{user.id}"}
            elif text.startswith("以后"):
                call = {
                    "name": "save_memory",
                    "args": {
                        "kind": "preference",
                        "field": "language",
                        "content": "zh-CN",
                        "evidence_message_ids": ["current"],
                    },
                    "id": f"save-{user.id}",
                }
            elif text.startswith("记住目标"):
                call = {
                    "name": "save_memory",
                    "args": {
                        "kind": "goal",
                        "content": "三年后购房",
                        "evidence_message_ids": ["current"],
                    },
                    "id": f"save-{user.id}",
                }
            else:
                call = None
            answer = (
                AIMessage(content="", tool_calls=[call])
                if call
                else AIMessage(
                    content=json.dumps(
                        {
                            "previous_tools": sum(
                                isinstance(item, ToolMessage) for item in messages
                            ),
                            "summaries": sum(
                                item.additional_kwargs.get("lc_source") == "summarization"
                                for item in messages
                            ),
                            "profile_seen": "zh-CN" in str(messages[0].content),
                        }
                    )
                )
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


settings = FinanceClawSettings(_env_file=None)
resources = build_resources(settings, enable_persistence=True)
service = LongTermMemoryService(
    conversation_repository=resources.conversation_repository,
    audit=resources.audit,
    outbox=resources.outbox_repository,
)
budget = ContextBudget(**settings.context_budget)
model = ScriptModel()
root = create_agent(
    model,
    tools=[external_mcp_snapshot, SaveMemoryTool(service)],
    context_schema=ExecutionContext,
    middleware=[
        NativeContextMiddleware(
            budget, resources.conversation_repository, resources.artifact_service, ScriptModel()
        ),
        MemoryRecallMiddleware(service),
        HumanInTheLoopMiddleware(
            interrupt_on={
                "save_memory": {
                    "allowed_decisions": ["approve", "reject"],
                    "when": lambda request: service.requires_approval(
                        trusted_context(request.runtime), request.tool_call["args"]
                    ),
                }
            }
        ),
        ToolResultArtifactMiddleware(resources.artifact_service),
        ToolContextEditingMiddleware(resources.artifact_service, budget),
        FinalContextMiddleware(RequestRecorder(budget, resources.conversation_repository)),
    ],
)
