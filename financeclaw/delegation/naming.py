"""Stable model-visible names for internal delegation capabilities."""

from .models import DelegationKind


def delegation_tool_name(kind: DelegationKind | str, target_id: str) -> str:
    normalized = DelegationKind(kind)
    return f"delegate_{normalized.value}__{target_id}"
