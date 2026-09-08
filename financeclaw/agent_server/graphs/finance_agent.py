"""Agent Server 侧的顶层 graph 工厂函数。

位于 orchestration/graphs 图装配层：提供顶层金融 ReAct Agent 与直连
工具图两个延迟装配工厂。当前 langgraph.json 注册的是 server_graphs
模块级 graph；只有显式改用工厂入口时才调用本模块，不应从业务模块导入装配。
"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.graphs.direct_tool import build_direct_tool_graph
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


def make_finance_agent(config: RunnableConfig) -> Any:
    """按当前组件目录的默认档案装配顶层金融 ReAct Agent graph。

    使用场景：Agent Server 注册顶层助手时调用；完成设置加载、组件
    装配，离线模式下换用本地确定性模型，最终按默认档案装配 Agent。

    Args:
        config: Agent Server 传入的运行配置；本工厂不消费，显式丢弃。

    Returns:
        装配完成的顶层 ReAct Agent（LangGraph 编译结果）。

    """
    del config
    settings = FinanceClawSettings()
    components = build_components(settings, enable_persistence=True)
    model = OfflineFinanceModel() if settings.offline_model else None
    return components.agent_factory.build(
        components.default_agent_profile,
        model=model,
        checkpointer=None,
    )


def make_direct_tool_graph(config: RunnableConfig) -> Any:
    """装配内部直连工具调用 graph。

    使用场景：Agent Server 注册直连工具助手时调用；用与顶层 Agent
    同源的组件集合装配 direct_tool 图，保证治理与审计口径一致。
    用户消息中的 ``/tool`` 是顶层 Agent 的调用偏好，不直接路由到此图。

    Args:
        config: Agent Server 传入的运行配置；本工厂不消费，显式丢弃。

    Returns:
        编译后的直连工具图（见 direct_tool.build_direct_tool_graph）。

    """
    del config
    settings = FinanceClawSettings()
    components = build_components(settings, enable_persistence=True)
    return build_direct_tool_graph(
        catalog=components.tool_catalog,
        policy=components.tool_policy,
        audit=components.audit,
        checkpointer=None,
        read_max_attempts=settings.read_max_attempts,
        artifact_service=components.artifact_service,
    )
