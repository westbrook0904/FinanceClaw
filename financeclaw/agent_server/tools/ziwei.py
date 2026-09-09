"""完整业务参数的只读排盘 Tool；校验和计算在同一次 function call 内完成。"""

import asyncio
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.ziwei import (
    BirthInput,
    ChartLevel,
    Focus,
    TargetSelector,
    ZiweiAnalysisRequest,
)
from financeclaw.shared.releases.tools import ziwei_tool_governance


class ZiweiChartInput(ZiweiAnalysisRequest):
    """完整排盘参数；运行时由 ToolNode 注入，不进入模型可填写的 Schema。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime[ExecutionContext]


class ZiweiChartTool(BaseTool):
    """一个完整 Schema 支持所有排盘层级，避免重复传五份出生参数定义。"""

    name: str = "ziwei_chart"
    description: str = (
        "按出生资料计算紫微盘面。直接结合用户原请求、澄清问题与回答和授权引用填写参数。"
        "未知资料留空，工具会汇总缺失或歧义，不得编造。"
        "今年、明年等使用 target.kind=relative_period 与 unit/offset，由固定请求时钟解析。"
        "流运盘包含上层依据，无需逐层调用。"
    )
    args_schema: type[BaseModel] = ZiweiChartInput
    service: Any = Field(exclude=True, repr=False)

    def _run(
        self,
        question: str = "",
        subject_label: str = "本次排盘对象",
        mode: str = "interpretation",
        birth: BirthInput | None = None,
        target: TargetSelector | None = None,
        level: ChartLevel = ChartLevel.NATAL,
        focus: Focus = "overall",
        *,
        runtime: ToolRuntime[ExecutionContext],
    ) -> Command:
        """完成类型化参数解析后，聚合业务问题并生成绑定当前调用的真实证据。"""
        if self.service is None:
            raise ZiweiError("ZIWEI_ENGINE_UNAVAILABLE", "紫微候选功能尚未启用。")
        context = ExecutionContext.model_validate(runtime.context)
        self.service.authorize(context)
        request = ZiweiAnalysisRequest(
            question=question,
            subject_label=subject_label,
            mode=mode,
            birth=birth or BirthInput(),
            target=target,
            level=level,
            focus=focus,
        )
        birth, target = self.service.validate_input(request, context)
        projection = self.service.calculate(birth, target, request.level, request.focus, context)
        return Command(
            update={
                "ziwei_evidence": [
                    {
                        "tool_call_id": runtime.tool_call_id,
                        "request": request.model_dump(mode="json"),
                        "birth_fingerprint": birth.fingerprint,
                        "convention_ref": birth.convention_ref,
                        "target": target.model_dump(mode="json") if target else None,
                        "projection": projection.model_dump(mode="json"),
                    }
                ],
                "messages": [
                    ToolMessage(
                        content=projection.model_dump_json(),
                        tool_call_id=runtime.tool_call_id,
                        name=self.name,
                        additional_kwargs={"preserve_structure": True},
                    )
                ],
            }
        )

    async def _arun(self, **arguments) -> Command:
        """同步排盘与持久化在线程池中执行，复用同一固定输入契约。"""
        return await asyncio.to_thread(self._run, **arguments)


def ziwei_tools(service) -> tuple[ManagedTool, ...]:
    """注册统一只读排盘工具，运行身份与预算不暴露给模型。"""
    return (
        ManagedTool(tool=ZiweiChartTool(service=service), governance=ziwei_tool_governance()[0]),
    )
