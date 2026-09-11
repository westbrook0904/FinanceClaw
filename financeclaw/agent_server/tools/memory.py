"""统一的记忆保存与删除工具；证据策略由服务端决定。"""

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from langgraph.types import Command
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PrivateAttr

from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.agent_server.memory.models import MemoryDraft, MemoryType
from financeclaw.agent_server.memory.profiles import ProfileField
from financeclaw.agent_server.memory.service import MemoryReceiptPending
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.releases.tools import memory_tool_governance


class MemoryToolInput(BaseModel):
    """身份与 Store 仅由原生 ToolRuntime 注入。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    runtime: ToolRuntime[ExecutionContext]


class SaveMemoryInput(MemoryToolInput):
    """一个字段或事件一次保存，确认值随原生工具调用绑定。"""

    kind: MemoryType
    field: ProfileField | None = None
    valid_until: AwareDatetime | None = None
    content: str = Field(min_length=1, max_length=2000)
    evidence_message_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    supersedes_id: str | None = None


class SearchMemoriesInput(MemoryToolInput):
    """主动追加事件搜索的有界参数。"""

    query: str | None = Field(default=None, max_length=512)
    kinds: tuple[MemoryType, ...] | None = None
    limit: int = Field(default=6, ge=1, le=20)


class ForgetMemoryInput(MemoryToolInput):
    """区分退出召回与物理删除 Store 记录。"""

    memory_id: str = Field(min_length=1, max_length=128)
    mode: Literal["revoke", "delete"] = "revoke"


class _MemoryTool(BaseTool):
    """记忆工具共享可信身份、错误和原生 state 回执处理。"""

    handle_tool_error: bool = True
    _service: Any = PrivateAttr()

    def __init__(self, service, **kwargs):
        """注入经过治理的记忆服务。"""
        super().__init__(**kwargs)
        self._service = service

    @staticmethod
    def _failure(error):
        """将领域错误转换成不泄露内部状态的工具错误。"""
        return ToolException(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": getattr(error, "reason", "memory_operation_failed"),
                    "message": str(error),
                },
                ensure_ascii=False,
            )
        )

    def _receipt(self, runtime, result, *, forget=False, pending=False):
        """把操作结果与召回失效标志一起写回原生 state。"""
        update = {
            "memory_invalidated": True,
            "messages": [
                ToolMessage(
                    name=self.name,
                    tool_call_id=runtime.tool_call_id,
                    content=json.dumps(result, ensure_ascii=False),
                    status="error" if pending else "success",
                )
            ],
        }
        if forget:
            update["memory_forget_requested"] = True
        return Command(update=update)

    def _pending_receipt(self, runtime, error, *, forget=False):
        """部分成功同样使旧上下文失效，但明确返回待补齐的错误回执。"""
        return self._receipt(
            runtime,
            {"status": "receipt_pending", "reason": error.reason, "message": str(error)},
            forget=forget,
            pending=True,
        )


class SaveMemoryTool(_MemoryTool):
    """统一保存入口，由原生 HITL 处理一次必要确认。"""

    name: str = "save_memory"
    description: str = (
        "Save one explicit lasting user preference or confirmed event with user Journal evidence. "
        "Use evidence_message_ids=['current'] for the current user message. Profile fields use "
        "normalized values: language=zh-CN/en, verbosity=concise/detailed, "
        "output_format=table/markdown/bullets. "
        "Never save temporary instructions or inferred traits. "
        "The platform automatically saves verified explicit low-risk preferences and requests "
        "one native approval for other changes; do not ask for another verbal confirmation."
    )
    args_schema: type[BaseModel] = SaveMemoryInput

    def _run(
        self,
        kind,
        content,
        evidence_message_ids,
        field=None,
        supersedes_id=None,
        valid_until=None,
        *,
        runtime,
    ):
        """根据可信 ToolRuntime 执行记忆操作，并返回受控回执。"""
        try:
            result = self._service.save(
                trusted_context(runtime),
                runtime.store,
                draft=MemoryDraft(
                    kind=kind,
                    field=field,
                    valid_until=valid_until,
                    content=content,
                    evidence_message_ids=evidence_message_ids,
                ),
                mutation_id=runtime.tool_call_id,
                approved=True,
                supersedes_id=supersedes_id,
            )
        except MemoryReceiptPending as exc:
            return self._pending_receipt(runtime, exc)
        except Exception as exc:
            raise self._failure(exc) from exc
        return self._receipt(
            runtime,
            {
                "status": "saved",
                "memory_id": result.memory_id,
                "field": result.field,
                "revision": result.revision,
            },
        )


class SearchMemoriesTool(_MemoryTool):
    """显式搜索事件；常规画像由中间件确定读取。"""

    name: str = "search_memories"
    description: str = (
        "Search historical user-approved events when additional memory evidence is needed."
    )
    args_schema: type[BaseModel] = SearchMemoriesInput

    def _run(self, query=None, kinds=None, limit=6, *, runtime):
        """根据可信 ToolRuntime 执行记忆操作，并返回受控回执。"""
        try:
            items = self._service.search(
                trusted_context(runtime), runtime.store, query=query, kinds=kinds, limit=limit
            )
        except Exception as exc:
            raise self._failure(exc) from exc
        return json.dumps(
            {
                "memories": [
                    {
                        "memory_id": item.record.memory_id,
                        "content": item.record.content,
                        "reason": item.reason,
                    }
                    for item in items
                ]
            },
            ensure_ascii=False,
        )


class ForgetMemoryTool(_MemoryTool):
    """删除/撤销后立即使本轮召回与旧工作摘要失效。"""

    name: str = "forget_memory"
    description: str = (
        "Revoke a memory or delete its Store body and index. "
        "Original conversation deletion is separate."
    )
    args_schema: type[BaseModel] = ForgetMemoryInput

    def _run(self, memory_id, mode="revoke", *, runtime):
        """根据可信 ToolRuntime 执行记忆操作，并返回受控回执。"""
        try:
            result = self._service.forget(
                trusted_context(runtime), runtime.store, memory_id, mode=mode
            )
        except MemoryReceiptPending as exc:
            return self._pending_receipt(runtime, exc, forget=True)
        except Exception as exc:
            raise self._failure(exc) from exc
        return self._receipt(runtime, result, forget=True)


def default_memory_tools(service) -> tuple[ManagedTool, ...]:
    """实现与静态发布声明一一对应。"""
    implementations = (
        SearchMemoriesTool(service),
        SaveMemoryTool(service),
        ForgetMemoryTool(service),
    )
    return tuple(
        ManagedTool(tool, governance)
        for tool, governance in zip(implementations, memory_tool_governance(), strict=True)
    )
