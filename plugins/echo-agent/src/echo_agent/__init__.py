"""用于验证 Harness Agent 插件链路的最小回显插件。"""

from .agent import EchoAgent
from .plugin import EchoAgentPlugin

__all__ = ["EchoAgent", "EchoAgentPlugin"]
