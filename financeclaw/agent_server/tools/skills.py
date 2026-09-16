"""技能工具只接受业务选择，版本、身份与访问来源由运行时注入。"""

import asyncio
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import Field, PrivateAttr

from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.memory import MemoryToolInput
from financeclaw.kernel.skills import SkillError
from financeclaw.shared.releases.skills import skill_tool_governance
from financeclaw.shared.skills.access import ACCESS_KEY, RESOURCE_KEY
from financeclaw.shared.skills.packages import canonical


class LoadSkillInput(MemoryToolInput):
    """模型只能选择 Profile 内的 skill ID，不得指定 explicit 或版本。"""

    skill_id: str = Field(max_length=64)


class ReadSkillResourceInput(LoadSkillInput):
    """相对包路径及有界快照游标，禁止 URL 和宿主文件路径。"""

    resource_path: str = Field(max_length=256)
    cursor: str | None = Field(default=None, max_length=1024)


class SkillTool(BaseTool):
    """将领域服务的候选更新交给原生 ToolNode 提交。"""

    _service: Any = PrivateAttr(default=None)

    def __init__(self, service, **kwargs):
        """按 Agent 工厂绑定服务，目录占位实例不可直接激活。"""
        super().__init__(**kwargs)
        self._service = service

    def _run(self, *, runtime, skill_id, **arguments):
        """成功保留 Command 更新，失败回执与计量仍由原生图保存。"""
        try:
            if self._service is None:
                raise SkillError()
            if self.name == "load_skill":
                return Command(
                    update=self._service.activate(
                        runtime, runtime.state, skill_id, call_id=runtime.tool_call_id
                    )
                )
            text, ref = self._service.read_resource(
                runtime,
                runtime.state,
                skill_id,
                arguments["resource_path"],
                arguments.get("cursor"),
            )
            return ToolMessage(
                content=text,
                name=self.name,
                tool_call_id=runtime.tool_call_id,
                additional_kwargs={ACCESS_KEY: [ref], RESOURCE_KEY: [ref]},
            )
        except SkillError as exc:
            message = ToolMessage(
                content=canonical(exc.payload()),
                status="error",
                name=self.name,
                tool_call_id=runtime.tool_call_id,
            )
            return Command(update={**getattr(exc, "state_update", {}), "messages": [message]})

    async def _arun(self, *, runtime, skill_id, **arguments):
        """本地文件和 SQL/摘要准备离开 AgentServer 事件循环。"""
        return await asyncio.to_thread(self._run, runtime=runtime, skill_id=skill_id, **arguments)


def skill_tools(service):
    """固定两个受治理入口，加载回执保留结构且激活批次独占。"""
    return tuple(
        ManagedTool(
            SkillTool(
                service,
                name=g.tool_id,
                args_schema=LoadSkillInput if g.tool_id == "load_skill" else ReadSkillResourceInput,
                description=(
                    "Load a published skill by ID. Call alone in its tool batch. The body will be "
                    "available on the next model request; loading does not complete the task."
                    if g.tool_id == "load_skill"
                    else "Read a UTF-8 reference or template in an active skill by relative path. "
                    "Follow next_cursor for more. This never executes scripts."
                ),
                metadata={"preserve_result": g.tool_id == "load_skill", "skill_tool": True},
            ),
            g,
        )
        for g in skill_tool_governance()
    )
