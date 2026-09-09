"""五个只读排盘 Tool；资料来自当前 child state，不让模型重复填写生日。"""

import asyncio
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from financeclaw.agent_server.domains.ziwei.application import ZiweiService
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.ziwei import (
    LEVELS,
    BirthContext,
    ChartLevel,
    Focus,
    ResolvedTarget,
    ZiweiAnalysisRequest,
)
from financeclaw.shared.releases.tools import ziwei_tool_governance


class ZiweiToolInput(BaseModel):
    """仅允许当前任务主题；目标和出生资料从已冻结的 runtime 注入。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    # focus 只能省略或与冻结请求一致；ToolRuntime 由框架注入，不向模型索要身份。
    focus: Focus | None = None
    runtime: ToolRuntime[ExecutionContext]


class ZiweiChartTool(BaseTool):
    """统一薄门面，无当前用户或当前命盘的共享可变状态。"""

    args_schema: type[BaseModel] = ZiweiToolInput
    # 每个实例在装配时固定一个分析层级；共享 service 不缓存当前用户资料。
    level: ChartLevel
    service: Any = Field(exclude=True, repr=False)

    def _run(
        self, *, runtime: ToolRuntime[ExecutionContext], focus: Focus | None = None
    ) -> Command:
        """保存真实计算证据到 graph state，返回完整且可识别的结构化消息。"""
        if self.service is None:
            raise ZiweiError("ZIWEI_ENGINE_UNAVAILABLE", "紫微候选功能尚未启用。")
        context = ExecutionContext.model_validate(runtime.context)
        self.service.authorize(context)
        request = ZiweiAnalysisRequest.model_validate(runtime.state["ziwei_request"])
        if LEVELS.index(self.level) > LEVELS.index(request.level) or (
            focus and focus != request.focus
        ):
            raise ZiweiError("ZIWEI_RANGE_LIMIT", "工具请求超出本次子图调用的层级或主题。")
        birth = BirthContext.model_validate(runtime.state["ziwei_birth"])
        raw_target = runtime.state.get("ziwei_target")
        target = (
            ResolvedTarget.model_validate(raw_target)
            if raw_target and self.level is not ChartLevel.NATAL
            else None
        )
        projection = self.service.calculate(birth, target, self.level, request.focus, context)
        # 同一份证据同时写入 checkpoint 与 ToolMessage，finalize 才能检查
        # 模型引用的事实确实来自本次计算，而不是仅存在于未展示的内部 state。
        return Command(
            update={
                "ziwei_charts": [projection.model_dump(mode="json")],
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

    async def _arun(
        self, *, runtime: ToolRuntime[ExecutionContext], focus: Focus | None = None
    ) -> Command:
        """计算和 Artifact 存储在工作线程运行，不阻塞 Agent Server 事件循环。"""
        return await asyncio.to_thread(self._run, runtime=runtime, focus=focus)


def ziwei_tools(service: ZiweiService | None) -> tuple[ManagedTool, ...]:
    """即使默认未启用也可注册无副作用 Schema；根白名单控制可见性。"""
    result = []
    for level in LEVELS:
        name = f"ziwei_{level.value}_chart"
        result.append(
            ManagedTool(
                tool=ZiweiChartTool(
                    name=name,
                    level=level,
                    service=service,
                    description=(
                        f"Calculate {level.value} and ancestor charts using the frozen birth "
                        "and target in this Worker invocation. Do not call lower levels first. "
                        "To change birth or target, return to the root conversation."
                    ),
                ),
                governance=ziwei_tool_governance()[LEVELS.index(level)],
            )
        )
    return tuple(result)
