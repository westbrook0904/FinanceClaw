"""为受控摘要调用添加独立容量、隐私边界和实际用量计量。"""

import asyncio
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


class MeteredSummaryModel(BaseChatModel):
    """每次真正调用摘要模型时记录一次，并消耗持久根执行预算。"""

    delegate: BaseChatModel
    recorder: Any = Field(exclude=True)
    execution_context: Any = Field(exclude=True)
    execution: Any = Field(default=None, exclude=True)
    privacy_epoch_reader: Any = Field(default=None, exclude=True)
    expected_privacy_epoch: int | None = None
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
        if self.privacy_epoch_reader is not None and (
            self.privacy_epoch_reader(context) != self.expected_privacy_epoch
        ):
            raise ValueError("privacy epoch changed before summary request")
        if self.execution is not None and context.turn_id:
            self.execution.verify_context(context)
            self.execution.consume(context.turn_id, "model")
        manifest = self.recorder.record(context, self.delegate, messages, subtype="summary")
        if self.privacy_epoch_reader is not None and (
            self.privacy_epoch_reader(context) != self.expected_privacy_epoch
        ):
            raise ValueError("privacy epoch changed before summary transmission")
        return manifest

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """调用配置的摘要模型，保留上层 tracing callbacks。"""
        manifest = self._prepare(messages)
        response = self.delegate.invoke(
            messages,
            stop=stop,
            config={
                "callbacks": run_manager.handlers if run_manager else None,
                "metadata": run_manager.metadata if run_manager else {},
            },
        )
        self.recorder.observe(manifest, response)
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """异步执行同一摘要计量和模型调用路径。"""
        manifest = await asyncio.to_thread(self._prepare, messages)
        response = await self.delegate.ainvoke(
            messages,
            stop=stop,
            config={
                "callbacks": run_manager.handlers if run_manager else None,
                "metadata": run_manager.metadata if run_manager else {},
            },
        )
        await asyncio.to_thread(self.recorder.observe, manifest, response)
        return ChatResult(generations=[ChatGeneration(message=response)])
