"""把外部运行目标解析为固定版本的 Agent Server 调用参数。"""

from dataclasses import dataclass
from typing import Any

from financeclaw.kernel import AgentTarget, RunRequest, ToolTarget, WorkflowTarget
from financeclaw.modules.workflows import WorkflowCatalog
from financeclaw.orchestration.agents import AgentProfileCatalog
from financeclaw.orchestration.tools import ToolCatalog


class TargetResolutionError(LookupError):
    """定义TargetResolutionError。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """定义已固定版本并可提交执行的目标。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        kind: 记录或目标的语义类别。
        assistant_id: 提交 Agent Server 时使用的助手或图标识。
        input: 提交给运行目标的结构化输入。
        target_id: 解析前或解析后的目标稳定标识。
        target_version: 运行实际绑定的目标版本，防止后续配置变化影响重放。
    """

    kind: str
    assistant_id: str
    input: dict[str, Any]
    target_id: str
    target_version: str


class TargetResolver:
    """把外部目标请求解析为固定版本、可直接提交给 Agent Server 的目标。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        tool_catalog: 登记并解析所有可用受治理工具版本的目录。
        agent_profiles: 可按稳定标识和版本解析 Agent 配置的只读目录。
        workflow_catalog: 登记并解析所有可用确定性工作流版本的目录。
        default_agent_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        default_agent_version: 运行固定使用的版本，用于审计复现。
    """

    def __init__(
        self,
        *,
        tool_catalog: ToolCatalog,
        agent_profiles: AgentProfileCatalog,
        workflow_catalog: WorkflowCatalog | None = None,
        default_agent_id: str = "finance_agent",
        default_agent_version: str = "1.0.0",
    ) -> None:
        """注入并保存TargetResolver所需的协作对象，同时校验构造期不变量。"""
        self.tool_catalog = tool_catalog
        self.agent_profiles = agent_profiles
        self.workflow_catalog = workflow_catalog or WorkflowCatalog(())
        self.default_agent_id = default_agent_id
        self.default_agent_version = default_agent_version

    def resolve(self, request: RunRequest) -> ResolvedTarget:
        """校验目标类型和直接调用权限，解析固定版本并构造服务端输入。"""
        target = request.target
        if target is None:
            profile = self.agent_profiles.resolve(self.default_agent_id, self.default_agent_version)
            return ResolvedTarget(
                kind="agent",
                assistant_id="finance_agent",
                input={"messages": [{"role": "user", "content": request.message}]},
                target_id=profile.agent_id,
                target_version=profile.version,
            )
        if isinstance(target, AgentTarget):
            try:
                profile = self.agent_profiles.resolve(target.agent_id, target.version)
            except LookupError as exc:
                raise TargetResolutionError(str(exc)) from exc
            return ResolvedTarget(
                kind="agent",
                assistant_id="finance_agent",
                input={"messages": [{"role": "user", "content": request.message}]},
                target_id=profile.agent_id,
                target_version=profile.version,
            )
        if isinstance(target, ToolTarget):
            try:
                managed = self.tool_catalog.resolve(target.tool_id, target.version)
            except LookupError as exc:
                raise TargetResolutionError(str(exc)) from exc
            if not managed.governance.direct_invocation:
                raise TargetResolutionError(
                    f"tool is only available inside the governed Agent path: {target.tool_id}"
                )
            return ResolvedTarget(
                kind="tool",
                assistant_id="direct_tool",
                input={
                    "tool_id": managed.governance.tool_id,
                    "version": managed.governance.version,
                    "arguments": target.arguments,
                },
                target_id=managed.governance.tool_id,
                target_version=managed.governance.version,
            )
        if isinstance(target, WorkflowTarget):
            try:
                definition = self.workflow_catalog.resolve(target.workflow_id, target.version)
                normalized = definition.normalize_input(target.arguments)
            except (LookupError, ValueError) as exc:
                raise TargetResolutionError(str(exc)) from exc
            return ResolvedTarget(
                kind="workflow",
                assistant_id=definition.assistant_id,
                input=normalized,
                target_id=definition.workflow_id,
                target_version=definition.version,
            )
        raise TypeError("unsupported request target")
