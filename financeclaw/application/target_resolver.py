"""目标解析器：把 RunRequest 中的调用偏好指令解析为可直接执行的 ResolvedTarget。"""

from dataclasses import dataclass
from typing import Any

from financeclaw.kernel import AgentTarget, RunRequest, ToolTarget, WorkflowTarget
from financeclaw.modules.workflows import WorkflowCatalog
from financeclaw.orchestration.agents import AgentProfileCatalog
from financeclaw.orchestration.tools import ToolCatalog


class TargetResolutionError(LookupError):
    """目标无法解析或未被治理策略允许时抛出的查找类异常。"""

    pass


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """解析完成后可直接交给 Run 执行通道的目标描述。

    使用场景：RunService 在受理 RunRequest 前调用 TargetResolver.resolve，
    以解析结果确定服务端 assistant、输入载荷与目标元数据（kind/id/version）。

    Attributes:
        kind: 目标类型，取值 "agent"、"tool" 或 "workflow"。
        assistant_id: 执行该目标所用的服务端 assistant（编译图）标识。
        input: 发给服务端的输入载荷；Agent 为消息列表，工具为调用参数，
            工作流为按 input_schema 归一化后的业务参数。
        target_id: 目标业务 ID（Agent、工具或工作流的标识）。
        target_version: 目标语义化版本号。

    """

    kind: str
    assistant_id: str
    input: dict[str, Any]
    target_id: str
    target_version: str


class TargetResolver:
    """调用偏好解析器：把 /tool、/workflow、/agent 指令映射为执行目标。

    使用场景：BFF 不接受显式 Target 时，消息中的 `/tool <id>` 等指令是调用
    偏好；RunService 依赖本解析器决定顶层 Agent、直接工具或工作流执行通道，
    并强制执行治理约束（如工具是否允许直接调用）。

    Attributes:
        tool_catalog: 受管工具目录，用于解析工具目标及其治理策略。
        agent_profiles: Agent Profile 目录，用于解析 Agent 目标与默认顶层 Agent。
        workflow_catalog: 工作流目录；未提供时为空目录，工作流目标一律解析失败。
        default_agent_id: 未指定目标时的默认顶层 Agent ID。
        default_agent_version: 默认顶层 Agent 的 Profile 版本。

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
        """构建目标解析器并绑定各目录。

        Args:
            tool_catalog: 受管工具目录。
            agent_profiles: Agent Profile 目录。
            workflow_catalog: 工作流目录；None 时使用空目录。
            default_agent_id: 未指定目标时的默认顶层 Agent ID。
            default_agent_version: 默认顶层 Agent 的 Profile 版本。

        """
        self.tool_catalog = tool_catalog
        self.agent_profiles = agent_profiles
        self.workflow_catalog = workflow_catalog or WorkflowCatalog(())
        self.default_agent_id = default_agent_id
        self.default_agent_version = default_agent_version

    def resolve(self, request: RunRequest) -> ResolvedTarget:
        """按请求中的目标类型解析执行目标，未指定时回落默认顶层 Agent。

        Args:
            request: 待解析的 Run 请求，target 可为 None。

        Returns:
            解析后的执行目标描述。

        Raises:
            TargetResolutionError: 目标不存在、版本缺失或治理策略不允许。
            TypeError: 目标类型不受支持。

        """
        target = request.target
        # 1. 未指定目标：回落默认顶层 Agent，输入即用户消息。
        if target is None:
            profile = self.agent_profiles.resolve(self.default_agent_id, self.default_agent_version)
            return ResolvedTarget(
                kind="agent",
                assistant_id="finance_agent",
                input={"messages": [{"role": "user", "content": request.message}]},
                target_id=profile.agent_id,
                target_version=profile.version,
            )
        # 2. 显式 Agent 目标：解析 Profile 固定版本，仍由顶层 finance_agent 图执行。
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
        # 3. 工具目标：解析受管工具并校验治理策略允许直接调用。
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
        # 4. 工作流目标：解析已发布定义并按 input_schema 归一化入参。
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
        # 5. 其余类型一律拒绝。
        raise TypeError("unsupported request target")
