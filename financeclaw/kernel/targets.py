"""运行目标（RunTarget）契约：声明一次运行要触达的 Tool、Workflow 或 Agent。

本模块属于 kernel（稳定共享契约层），供请求模型与 orchestration 共用；
通过 ``kind`` 字段做判别联合，保证三类目标可校验、可序列化。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# 目标 ID：1~128 位，仅允许字母、数字与 . _ : -，与 context.Identifier 保持同一口径。
TargetId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
# 语义化版本号（major.minor.patch），用于目标能力解析时的版本锁定。
Version = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^\d+\.\d+\.\d+$")]


class ToolTarget(BaseModel):
    """以受治理 Tool 为目标的运行描述，用于直连调用某个工具。

    使用场景：用户通过斜杠指令或 API 明确请求调用某工具时，请求模型将
    目标解析为 ``RunTarget`` 判别联合中的 ``tool`` 分支。

    Attributes:
        kind: 判别字段，恒为 ``"tool"``。
        tool_id: 目标工具 ID，须在 ToolCatalog 中已注册。
        version: 指定的工具版本；为 None 时解析为目录中的最新版本。
        arguments: 工具入参字典，键为参数名，取值须符合工具参数 schema。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool"] = "tool"
    tool_id: TargetId
    version: Version | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class WorkflowTarget(BaseModel):
    """以已发布 Workflow 为目标的运行描述，用于直连执行一个多步流程。

    使用场景：集成方需要确定性的流程执行（而非 Agent 自由决策）时，
    将目标解析为 ``RunTarget`` 判别联合中的 ``workflow`` 分支。

    Attributes:
        kind: 判别字段，恒为 ``"workflow"``。
        workflow_id: 目标流程定义 ID，须在 WorkflowCatalog 中已注册并发布。
        version: 指定的流程版本；为 None 时解析为最新已发布版本。
        arguments: 流程入参字典，键为参数名，取值须符合流程入参 schema。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workflow"] = "workflow"
    workflow_id: TargetId
    version: Version | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class AgentTarget(BaseModel):
    """以某个 Agent 为目标的运行描述，用于把任务路由到指定 Agent。

    使用场景：多 Agent 场景下显式指定目标 Agent（如斜杠指令）时使用；
    本模型不含 arguments，Agent 依据会话历史与自身提示词自行决策。

    Attributes:
        kind: 判别字段，恒为 ``"agent"``。
        agent_id: 目标 Agent ID，须在 AgentProfileCatalog 中已注册。
        version: 指定的 Agent 版本；为 None 时解析为目录中的最新版本。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"] = "agent"
    agent_id: TargetId
    version: Version | None = None


# 运行目标判别联合类型：按 ``kind`` 字段区分 Tool/Workflow/Agent 三类目标。
RunTarget = Annotated[ToolTarget | WorkflowTarget | AgentTarget, Field(discriminator="kind")]
