"""复用原生工具清理，在每条被清理结果中保留平台工件引用。"""

import asyncio
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.context_editing import ClearToolUsesEdit, ContextEditingMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.agent_server.context.artifacts import ToolResultArchive
from financeclaw.agent_server.context.budget import TokenCounter
from financeclaw.agent_server.context.turns import trusted_context


@dataclass
class ArchivedToolEdit:
    """原生编辑决定清理范围，适配层只补归档与逐条保护。"""

    archive: ToolResultArchive
    context: object
    trigger: int
    keep: int

    def apply(self, messages, *, count_tokens):
        """所有归档完成后才把编辑结果交给后续模型调用。"""
        originals = list(messages)
        protected_names = {
            item.name
            for item in messages
            if isinstance(item, ToolMessage)
            and item.additional_kwargs.get("preserve_structure")
            and item.name
        }
        ClearToolUsesEdit(
            trigger=self.trigger, keep=self.keep, exclude_tools=tuple(protected_names)
        ).apply(messages, count_tokens=count_tokens)
        for index, (original, edited) in enumerate(zip(originals, messages, strict=True)):
            if not isinstance(original, ToolMessage) or original.content == edited.content:
                continue
            if original.additional_kwargs.get("preserve_structure"):
                messages[index] = original
                continue
            projected = self.archive.project(original, self.context)
            messages[index] = projected.model_copy(
                update={"response_metadata": edited.response_metadata}
            )


class ToolContextEditingMiddleware(AgentMiddleware):
    """每次请求构造无共享可变上下文的原生编辑器。"""

    def __init__(self, service, budget):
        """配置原生清理器需要的归档服务与上下文预算。"""
        self.archive = ToolResultArchive(service)
        self.budget = budget
        self.counter = TokenCounter()

    def _editor(self, request):
        """为本次可信运行构建独立的原生工具结果编辑器。"""
        return ContextEditingMiddleware(
            edits=[
                ArchivedToolEdit(
                    self.archive,
                    trusted_context(request.runtime),
                    min(self.budget.soft_input_tokens, self.budget.available_input_tokens),
                    self.budget.tool_results_to_keep,
                )
            ],
            token_counter=lambda messages: sum(self.counter.message(item) for item in messages),
        )

    def wrap_model_call(self, request, handler):
        """原生编辑器负责请求副本与 handler 组合。"""
        return self._editor(request).wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        """先在线程中归档/投影，再调用异步模型 handler。"""
        prepared = await asyncio.to_thread(
            self._editor(request).wrap_model_call,
            request,
            lambda value: value,
        )
        return await handler(prepared)
