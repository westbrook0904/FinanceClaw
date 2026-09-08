"""Agent 发布快照的固定与一致性校验；协调端与执行端共用。"""

from typing import Any

from financeclaw.shared.execution_ledger.repository import ExecutionConflict


def agent_snapshot(
    profile: Any, context: Any, *, thread_id: str, input_hash: str
) -> dict[str, Any]:
    """固定实际档案、Schema、工具与执行身份，恢复时可与当前发布物比对。"""
    return {
        "context": context.model_dump(mode="json"),
        "thread_id": thread_id,
        "assistant_id": profile.execution_assistant_id,
        "input_hash": input_hash,
        "profile": profile.model_dump(mode="json"),
        "input_schema": profile.input_schema.model_json_schema() if profile.input_schema else None,
        "output_schema": profile.output_schema.model_json_schema()
        if profile.output_schema
        else None,
        "limits": {
            "model": profile.max_tree_model_calls,
            "tool": profile.max_tree_tool_calls,
            "operation": profile.max_tree_operations,
        },
    }


def verify_agent_snapshot(profile: Any, snapshot: dict[str, Any]) -> None:
    """旧代码与依赖不能共存时阻止恢复，不将旧版本记录静默交给新图。"""
    if (
        snapshot.get("profile") != profile.model_dump(mode="json")
        or snapshot.get("assistant_id") != profile.execution_assistant_id
        or snapshot.get("input_schema")
        != (profile.input_schema.model_json_schema() if profile.input_schema else None)
        or snapshot.get("output_schema")
        != (profile.output_schema.model_json_schema() if profile.output_schema else None)
    ):
        raise ExecutionConflict("pinned Agent release is unavailable; drain or reauthorize the run")
