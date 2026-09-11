"""给原生摘要模型添加容量与实际尝试计量，不复制框架摘要算法。"""

import asyncio
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


class MeteredSummaryModel(BaseChatModel):
    """每次原生 with_retry 真正调用模型时记录一次，并消耗根执行预算。"""

    delegate: BaseChatModel
    recorder: Any = Field(exclude=True)
    execution_context: Any = Field(exclude=True)
    execution: Any = Field(default=None, exclude=True)
    max_attempts: int = 3
    _attempts: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        """为摘要调用提供稳定的模型追踪类型。"""
        return "financeclaw-summary"

    @property
    def attempts(self) -> int:
        """当前摘要操作已经消耗的实际尝试数。"""
        return self._attempts

    def _prepare(self, messages):
        """每次实际尝试先校验并消耗根预算，再记录输入 Manifest。"""
        if self._attempts >= self.max_attempts:
            raise ValueError("summary attempt budget exhausted")
        self._attempts += 1
        context = self.execution_context
        if self.execution is not None and context.root_run_id:
            self.execution.verify_context(context)
            self.execution.consume(context.run_id, "model")
        self.recorder.record(context, self.delegate, messages, subtype="summary")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """调用配置的摘要模型，保留上层 tracing callbacks。"""
        self._prepare(messages)
        response = self.delegate.invoke(
            messages,
            stop=stop,
            config={
                "callbacks": run_manager.handlers if run_manager else None,
                "metadata": run_manager.metadata if run_manager else {},
            },
        )
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """异步执行同一摘要计量和模型调用路径。"""
        await asyncio.to_thread(self._prepare, messages)
        response = await self.delegate.ainvoke(
            messages,
            stop=stop,
            config={
                "callbacks": run_manager.handlers if run_manager else None,
                "metadata": run_manager.metadata if run_manager else {},
            },
        )
        return ChatResult(generations=[ChatGeneration(message=response)])
