"""Published Agent Server graph entry points.

Graph factories are loaded lazily so the composition root can import a
published workflow release without recursively importing itself through the
legacy convenience factories.
"""

from typing import Any

__all__ = ["build_direct_tool_graph", "make_direct_tool_graph", "make_finance_agent"]


def __getattr__(name: str) -> Any:
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
