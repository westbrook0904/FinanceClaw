"""跨层共享且不依赖具体实现的运行上下文、目标与响应契约。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TargetId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
Version = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^\d+\.\d+\.\d+$")]


class ToolTarget(BaseModel):
    """定义工具Target。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        kind: 记录或目标的语义类别。
        tool_id: 工具的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        arguments: 传给目标工具或工作流的已解析参数。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool"] = "tool"
    tool_id: TargetId
    version: Version | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class WorkflowTarget(BaseModel):
    """定义工作流Target。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        kind: 记录或目标的语义类别。
        workflow_id: 工作流的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        arguments: 传给目标工具或工作流的已解析参数。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workflow"] = "workflow"
    workflow_id: TargetId
    version: Version | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class AgentTarget(BaseModel):
    """定义AgentTarget。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        kind: 记录或目标的语义类别。
        agent_id: Agent 配置的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"] = "agent"
    agent_id: TargetId
    version: Version | None = None


RunTarget = Annotated[ToolTarget | WorkflowTarget | AgentTarget, Field(discriminator="kind")]
