"""Agent、Tool 与 Plugin 的公共扩展接口。

业务插件应面向本包和 ``harness_contracts`` 编程，
不得依赖 Runtime、Registry 或 Policy 的具体实现。
"""

from .agent import AgentSPI
from .capability import Capability
from .models import AgentRequest, PluginManifest, ToolRequest, validate_manifest_capabilities
from .plugin import PluginSPI
from .tool import ToolSPI

__all__ = [
    "AgentRequest",
    "AgentSPI",
    "Capability",
    "PluginManifest",
    "PluginSPI",
    "ToolRequest",
    "ToolSPI",
    "validate_manifest_capabilities",
]
