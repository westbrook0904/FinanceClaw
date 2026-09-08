"""AgentServer 发布物将共享 Workflow 声明绑定到已编译的图。"""

from dataclasses import dataclass
from typing import Any

from financeclaw.kernel.workflows.models import WorkflowRelease


@dataclass(frozen=True, slots=True)
class WorkflowDefinition(WorkflowRelease):
    """执行端的完整工作流定义；必须绑定实际编译图。"""

    graph: Any

    def __post_init__(self) -> None:
        """保留声明校验并拒绝没有执行图的发布物。"""
        WorkflowRelease.__post_init__(self)
        if self.graph is None:
            raise ValueError("published workflow requires a compiled graph")
