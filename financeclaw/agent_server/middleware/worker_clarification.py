"""Worker 缺资料时由根图汇总后原生中断，用户回答后继续同一根任务。"""

import asyncio

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from financeclaw.agent_server.middleware.middleware import _context
from financeclaw.agent_server.tools.subgraphs import SubagentTool
from financeclaw.shared.releases.interactions import CLARIFICATION_TOOL
from financeclaw.shared.turns.snapshots import verify_agent_snapshot
from financeclaw.shared.turns.types import ExecutionConflict, digest


class WorkerClarificationMiddleware(AgentMiddleware):
    """只读取当前已完成批次中、已固定发布的 Worker 公开结果。"""

    def __init__(self, catalog, profile, execution):
        """与根图使用同一工具目录、发布和持久授权仓储。"""
        self.catalog = catalog
        self.profile = profile
        self.execution = execution

    @hook_config(can_jump_to=["tools"])
    def before_model(self, state, runtime):
        """在下一次模型消费前派发单个根澄清工具，所有 Worker 回执已持久化。"""
        receipts = []
        for message in reversed(state.get("messages", [])):
            if not isinstance(message, ToolMessage):
                break
            receipts.append(message)
        if not receipts:
            return None
        previous = (
            state["messages"][-len(receipts) - 1]
            if len(state["messages"]) > len(receipts)
            else None
        )
        calls = (
            {call["id"]: call["name"] for call in previous.tool_calls}
            if isinstance(previous, AIMessage)
            else {}
        )
        questions, requests = [], []
        for receipt in reversed(receipts):
            if receipt.name == CLARIFICATION_TOOL and receipt.status == "error":
                raise ExecutionConflict("root clarification tool failed; no user answer received")
            if receipt.status != "success" or calls.get(receipt.tool_call_id) != receipt.name:
                continue
            managed = self.catalog.resolve(receipt.name)
            tool = managed.tool
            if not isinstance(tool, SubagentTool):
                continue
            if tool.declaration not in self.profile.worker_manifest:
                raise ExecutionConflict("clarification Worker release is not pinned by this root")
            try:
                public = tool.release.output_schema.model_validate_json(receipt.content)
            except (ValidationError, TypeError) as exc:
                raise ExecutionConflict("Worker clarification result violates its release") from exc
            if getattr(public, "outcome", None) == "needs_clarification":
                question = getattr(public, "question", None)
                if not question or not getattr(public, "missing_fields", None):
                    raise ExecutionConflict("Worker clarification requires a question and fields")
                requests.append(
                    {
                        "tool_call_id": receipt.tool_call_id,
                        "tool": receipt.name,
                        "subject_label": getattr(public, "subject_label", ""),
                        "missing_fields": list(public.missing_fields),
                        "question": question,
                    }
                )
                item = (getattr(public, "subject_label", ""), question)
                if item not in questions:
                    questions.append(item)
        if not questions:
            return None
        context = _context(runtime.context)
        if self.execution is None:
            raise ExecutionConflict("Worker clarification requires persistent root execution")
        self.execution.verify_context(context)
        execution = self.execution.get(context.turn_id)
        if execution["cancel_requested_at"]:
            raise ExecutionConflict("root cancellation requested")
        verify_agent_snapshot(self.profile, execution["release_snapshot"])
        # 多个对象分别提问；同对象相同问题去重。不得生成新的缺失字段值。
        content = (
            questions[0][1]
            if len(questions) == 1
            else "\n\n".join(
                f"{label}：{question}" if label else question for label, question in questions
            )
        )
        if len(content) > 2000:
            raise ExecutionConflict("root clarification exceeds the published question limit")
        # 调用 ID 取根身份与本批回执；重放不能产生另一个待回答实例。
        identifier = "clarification-" + digest([context.turn_id, requests])[:24]
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": CLARIFICATION_TOOL,
                            "args": {"question": content},
                            "id": identifier,
                        }
                    ],
                    additional_kwargs={"clarification_requests": requests},
                )
            ],
            "jump_to": "tools",
        }

    @hook_config(can_jump_to=["tools"])
    async def abefore_model(self, state, runtime):
        """异步入口在线程池复验授权，保持与同步入口相同的中断路由。"""
        return await asyncio.to_thread(self.before_model, state, runtime)
