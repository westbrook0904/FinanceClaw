"""为委派的子线程生成可复现且合法的稳定标识。"""

from .models import DelegationKind


def delegation_tool_name(kind: DelegationKind | str, target_id: str) -> str:
    """将委派类型与目标标识规范化为合法且稳定的工具名称。"""
    normalized = DelegationKind(kind)
    return f"delegate_{normalized.value}__{target_id}"
