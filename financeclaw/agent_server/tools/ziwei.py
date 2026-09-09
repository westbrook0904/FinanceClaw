"""完整业务参数的只读排盘 Tool；校验和计算在同一次 function call 内完成。"""

import asyncio
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.domains.ziwei.tool_inputs import analysis_request, public_input_error
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.ziwei_tools import (
    ZIWEI_TOOL_INPUTS,
    ZiweiDailyInput,
    ZiweiDecadalInput,
    ZiweiMonthlyInput,
    ZiweiNatalInput,
    ZiweiYearlyInput,
)
from financeclaw.shared.releases.tools import ziwei_tool_governance


class ZiweiRuntimeInput(BaseModel):
    """运行时由 ToolNode 注入，不进入模型可填写的 Schema。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    runtime: ToolRuntime[ExecutionContext]


class ZiweiNatalToolInput(ZiweiNatalInput, ZiweiRuntimeInput):
    """本命参数与可信运行时。"""


class ZiweiDecadalToolInput(ZiweiDecadalInput, ZiweiRuntimeInput):
    """大限参数与可信运行时。"""


class ZiweiYearlyToolInput(ZiweiYearlyInput, ZiweiRuntimeInput):
    """流年参数与可信运行时。"""


class ZiweiMonthlyToolInput(ZiweiMonthlyInput, ZiweiRuntimeInput):
    """流月参数与可信运行时。"""


class ZiweiDailyToolInput(ZiweiDailyInput, ZiweiRuntimeInput):
    """流日参数与可信运行时。"""


_TOOL_SCHEMAS = {
    "ziwei_natal_chart": (ZiweiNatalToolInput, "本命盘：只填写出生资料，不需要查询日期。"),
    "ziwei_decadal_chart": (
        ZiweiDecadalToolInput,
        "大限盘：用 on_date 定位该日所在大限；当前大限可填 day_offset=0。",
    ),
    "ziwei_yearly_chart": (
        ZiweiYearlyToolInput,
        "流年盘：year 查询公历整年；今年 year_offset=0，明年 year_offset=1。",
    ),
    "ziwei_monthly_chart": (
        ZiweiMonthlyToolInput,
        "流月盘：year/month 查询公历整月；本月 month_offset=0，下月为 1。",
    ),
    "ziwei_daily_chart": (
        ZiweiDailyToolInput,
        "流日盘：on_date 查询某日；今天 day_offset=0，明天为 1；逐日最多 31 天。",
    ),
}


class ZiweiChartTool(BaseTool):
    """五个命名工具共用计算与证据绑定实现，但各自只接受对应层级的参数。"""

    args_schema: type[BaseModel]
    service: Any = Field(exclude=True, repr=False)

    @property
    def tool_call_schema(self) -> type[BaseModel]:
        """直接暴露版本化业务契约，保留示例与额外字段约束，排除 runtime。"""
        return ZIWEI_TOOL_INPUTS[self.name]

    def _run(
        self,
        *,
        runtime: ToolRuntime[ExecutionContext],
        **arguments,
    ) -> Command:
        """完成类型化参数解析后，聚合业务问题并生成绑定当前调用的真实证据。"""
        if self.service is None:
            raise ZiweiError("ZIWEI_ENGINE_UNAVAILABLE", "紫微候选功能尚未启用。")
        context = ExecutionContext.model_validate(runtime.context)
        self.service.authorize(context)
        request = analysis_request(self.name, arguments)
        try:
            birth, target = self.service.validate_input(request, context)
            projection = self.service.calculate(
                birth, target, request.level, request.focus, context
            )
        except ZiweiError as error:
            raise public_input_error(error) from None
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
    """注册五个独立 Schema 的只读入口，运行身份与预算不暴露给模型。"""
    return tuple(
        ManagedTool(
            tool=ZiweiChartTool(
                name=governance.tool_id,
                args_schema=_TOOL_SCHEMAS[governance.tool_id][0],
                description=(
                    _TOOL_SCHEMAS[governance.tool_id][1]
                    + "结合原问题、澄清回答和授权资料填写出生参数；未知留空，不得编造。"
                    "不传 level 或 target；流运盘包含上层依据，无需逐层调用。"
                ),
                service=service,
            ),
            governance=governance,
        )
        for governance in ziwei_tool_governance()
    )
