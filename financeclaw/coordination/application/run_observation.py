"""归一化运行观察，任何未处理的中断都优先于 completed。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from financeclaw.coordination.execution.service import json_value
from financeclaw.kernel.delegation.models import HANDOFF_ADAPTER, HandoffRequest


@dataclass(frozen=True)
class RunObservation:
    """供会话、委派与工作流选择后续动作的统一运行观察。

    kind 优先表达挂起的 handoff、审批或资料交互，再表达执行终态；
    unsupported 表示无法安全识别的中断，调用方必须保留阻塞状态。
    payload 保存内部中断载荷，interrupt_id 用于精确恢复；仅 handoff
    分类会附带已验证的委派契约。此对象仍需投影后才能对用户公开。
    """

    kind: Literal[
        "running",
        "completed",
        "failed",
        "handoff",
        "hitl",
        "workflow",
        "interaction",
        "unsupported",
    ]
    payload: dict[str, Any] | None = None
    interrupt_id: str | None = None
    handoff: HandoffRequest | None = None


def observe_run(value: Mapping[str, Any]) -> RunObservation:
    """所有启动、查询、恢复、流结束路径共用的保守分类器。"""
    items = value.get("interrupts") or value.get("__interrupt__") or ()
    if isinstance(items, Mapping):
        items = (items,)
    if items:
        if not isinstance(items, (list, tuple)) or len(items) != 1:
            return RunObservation("unsupported")
        item = json_value(items[0])
        if not isinstance(item, Mapping):
            return RunObservation("unsupported")
        raw = item.get("value", item)
        if not isinstance(raw, Mapping):
            return RunObservation("unsupported")
        payload = dict(raw)
        identifier = item.get("id")
        if payload.get("kind") == "user_interaction" and payload.get("schema_version") == 1:
            if identifier and payload.get("interaction_kind") in {"input", "choice", "approval"}:
                return RunObservation("interaction", payload, identifier)
            return RunObservation("unsupported")
        if "handoff_id" in payload:
            try:
                handoff = HANDOFF_ADAPTER.validate_python(payload)
            except ValueError:
                return RunObservation("unsupported")
            return RunObservation("handoff", payload, identifier, handoff)
        if "action_requests" in payload:
            actions = payload["action_requests"]
            configs = payload.get("review_configs", ())
            if (
                isinstance(actions, list)
                and len(actions) == 1
                and isinstance(actions[0], Mapping)
                and "name" in actions[0]
                and isinstance(actions[0].get("args"), Mapping)
                and isinstance(configs, list)
                and len(configs) == 1
            ):
                return RunObservation("hitl", payload, identifier)
            return RunObservation("unsupported")
        if "approval_id" in payload and "workflow_id" in payload:
            return RunObservation("workflow", payload, identifier)
        return RunObservation("unsupported")
    status = str(value.get("status", "completed"))
    if status == "interrupted":
        return RunObservation("unsupported")
    if status in {"failed", "error", "timeout"}:
        return RunObservation("failed")
    if status in {"completed", "success", "rejected"}:
        return RunObservation("completed")
    return RunObservation("running")
