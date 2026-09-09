"""Worker 缺资料时由根图直接发问，不再让模型补造参数或继续执行。"""

import asyncio

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from financeclaw.agent_server.middleware.middleware import _context
from financeclaw.agent_server.tools.subgraphs import SubagentTool
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.snapshots import verify_agent_snapshot


class WorkerClarificationMiddleware(AgentMiddleware):
    """只读取当前已完成批次中、已固定发布的 Worker 公开结果。"""

    def __init__(self, catalog, profile, execution):
        """与根图使用同一工具目录、发布和持久授权仓储。"""
        self.catalog = catalog
        self.profile = profile
        self.execution = execution

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        """在下一次模型预算消费之前结束本轮，保留完整 Tool 回执。"""
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
        questions = []
        for receipt in reversed(receipts):
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
                item = (getattr(public, "subject_label", ""), question)
                if item not in questions:
                    questions.append(item)
        if not questions:
            return None
        context = _context(runtime.context)
        if self.execution is None:
            raise ExecutionConflict("Worker clarification requires persistent root execution")
        self.execution.verify_context(context)
        execution = self.execution.get(context.run_id)
        if execution["cancellation_requested"]:
            raise ExecutionConflict("root cancellation requested")
        verify_agent_snapshot(self.profile, execution["snapshot"])
        # 多个对象分别提问；同对象相同问题去重。不得生成新的缺失字段值。
        content = (
            questions[0][1]
            if len(questions) == 1
            else "\n\n".join(
                f"{label}：{question}" if label else question for label, question in questions
            )
        )
        return {"messages": [AIMessage(content=content)], "jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        """异步入口在线程池复验授权，保持与同步入口相同的终止语义。"""
        return await asyncio.to_thread(self.before_model, state, runtime)
