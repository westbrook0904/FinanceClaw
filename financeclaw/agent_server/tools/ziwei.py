"""完整业务参数的只读排盘 Tool；校验和计算在同一次 function call 内完成。"""

import asyncio
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.domains.ziwei.validation import schema_error
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.ziwei import ZiweiAnalysisRequest
from financeclaw.shared.releases.tools import ziwei_tool_governance


class ZiweiToolRuntimeInput(BaseModel):
    """仅供 ToolNode 识别可信运行时注入，不作为模型参数 Schema。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime[ExecutionContext]


class ZiweiChartTool(BaseTool):
    """一个完整 Schema 支持所有排盘层级，避免重复传五份出生参数定义。"""

    # 原生 JSON Schema 保留完整字段，同时让工具自己聚合校验问题。
    args_schema: dict[str, Any] = Field(default_factory=ZiweiAnalysisRequest.model_json_schema)
    service: Any = Field(exclude=True, repr=False)

    def get_input_schema(self, config=None):
        """运行时由框架注入；公开 function schema 仍使用 args_schema 的业务字段。"""
        return ZiweiToolRuntimeInput

    def _run(self, *, runtime: ToolRuntime[ExecutionContext], **arguments) -> Command:
        """先验证完整调用参数，再生成与本次调用绑定的真实证据。"""
        if self.service is None:
            raise ZiweiError("ZIWEI_ENGINE_UNAVAILABLE", "紫微候选功能尚未启用。")
        context = ExecutionContext.model_validate(runtime.context)
        self.service.authorize(context)
        try:
            request = ZiweiAnalysisRequest.model_validate(arguments)
        except ValidationError as error:
            raise schema_error(arguments, error) from None
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

    async def _arun(self, *, runtime: ToolRuntime[ExecutionContext], **arguments) -> Command:
        """同步排盘与持久化在线程池中执行，保留每个调用的原生作用域。"""
        return await asyncio.to_thread(self._run, runtime=runtime, **arguments)


def ziwei_tools(service) -> tuple[ManagedTool, ...]:
    """注册统一只读排盘工具，运行身份与预算不暴露给模型。"""
    return (
        ManagedTool(
            tool=ZiweiChartTool(
                name="ziwei_chart",
                service=service,
                description=(
                    "Calculate Ziwei charts using the supplied birth, level, target and focus. "
                    "Use task, user_context and authorized context_refs to fill the full schema. "
                    "Leave unknown facts empty; never invent birth details. "
                    "Include all known fields. The tool returns all identifiable input issues. "
                    "Each level includes ancestor facts; do not call every lower level first."
                ),
            ),
            governance=ziwei_tool_governance()[0],
        ),
    )
