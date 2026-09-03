"""委派工具的命名规则，供编排层把委派目标暴露为父 Agent 可调用的工具。

委派工具名是父 Agent 与委派子系统之间的约定标识：由委派种类与目标 ID 确定
性生成，同一目标在任何部署中都得到相同的工具名。
"""

from .models import DelegationKind


def delegation_tool_name(kind: DelegationKind | str, target_id: str) -> str:
    """按委派种类与目标 ID 生成确定性的委派工具名。

    结果形如 ``delegate_workflow__portfolio_review`` 或
    ``delegate_agent__market_research_agent``；同一目标恒定映射到同一工具名，
    便于幂等注册与审计追踪。

    Args:
        kind: 委派种类，接受 DelegationKind 成员或其字符串值。
        target_id: 目标标识，Workflow 为 workflow_id，Agent 为 agent_id。

    Returns:
        形如 ``delegate_<kind>__<target_id>`` 的工具名字符串。

    """
    normalized = DelegationKind(kind)
    return f"delegate_{normalized.value}__{target_id}"
