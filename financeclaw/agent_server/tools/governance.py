"""把执行端的 LangChain Tool 与共享治理契约绑定。"""

from dataclasses import dataclass

from langchain_core.tools import BaseTool

from financeclaw.kernel.tools import ToolGovernance


@dataclass(frozen=True, slots=True)
class ManagedTool:
    """治理受管 Tool：把 LangChain Tool 实现与其治理元数据成对绑定。

    使用场景：各类 Tool 实现完成装配后都包装为 ManagedTool 再注册进
    ToolCatalog；执行策略与审计层通过 governance 字段做放行判定与
    归因，通过 tool 字段做实际调用。

    Attributes:
        tool: LangChain ``BaseTool`` 实例，承担实际的工具执行。
        governance: 该 Tool 的治理元数据，其 tool_id 必须与 tool.name
            一致。

    """

    tool: BaseTool
    governance: ToolGovernance

    def __post_init__(self) -> None:
        """校验绑定不变式：tool 必须是 BaseTool 且名称与治理 ID 一致。

        Raises:
            TypeError: tool 不是 LangChain ``BaseTool`` 实例。
            ValueError: tool.name 与 governance.tool_id 不一致。

        """
        if not isinstance(self.tool, BaseTool):
            raise TypeError("tool must be a LangChain BaseTool")
        if self.tool.name != self.governance.tool_id:
            raise ValueError("BaseTool name must match governance tool_id")

    @property
    def key(self) -> tuple[str, str]:
        """返回目录索引键 (tool_id, version)。"""
        return self.governance.tool_id, self.governance.version
