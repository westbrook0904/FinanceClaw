"""orchestration/graphs 包的对外导出入口。

集中暴露本层可复用的 graph 工厂函数；因 LangGraph 装配依赖较重，
按 PEP 562 惰性导入，仅在实际取用属性时才加载对应模块。
"""

from typing import Any

# 包级公开 API：直连工具图与顶层 Agent 图的工厂函数。
__all__ = ["build_direct_tool_graph", "make_direct_tool_graph", "make_finance_agent"]


def __getattr__(name: str) -> Any:
    """按 PEP 562 惰性解析包级属性，避免导入本包即触发重量级装配。

    Args:
        name: 期望获取的属性名，须属于 ``__all__`` 登记的导出项。

    Returns:
        对应的 graph 工厂函数；``make_direct_tool_graph`` 与
        ``make_finance_agent`` 均来自 finance_agent 模块。

    Raises:
        AttributeError: 属性名不属于本包公开导出项时抛出。

    """
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
