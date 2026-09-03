"""Agent、工具和 LangGraph 工作流的运行时编排。"""

from typing import Any

__all__ = ["build_direct_tool_graph", "make_direct_tool_graph", "make_finance_agent"]


def __getattr__(name: str) -> Any:
    """按需导入并返回公开符号，避免包初始化阶段形成循环依赖。"""
    if name == "build_direct_tool_graph":
        from .direct_tool import build_direct_tool_graph

        return build_direct_tool_graph
    if name in {"make_direct_tool_graph", "make_finance_agent"}:
        from .finance_agent import make_direct_tool_graph, make_finance_agent

        return {
            "make_direct_tool_graph": make_direct_tool_graph,
            "make_finance_agent": make_finance_agent,
        }[name]
    raise AttributeError(name)
