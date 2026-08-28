"""Bounded Plan Repair 的安全反馈、哈希与 JSON 投影工具。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from harness_contracts import PlanningError
from harness_model import GenerateResult

from .models import PlanValidationIssue

MAX_REPAIR_ERRORS = 16
_MAX_REPAIR_JSON_DEPTH = 8
_MAX_REPAIR_COLLECTION_ITEMS = 64
_MAX_REPAIR_STRING_LENGTH = 512
_MAX_REPAIR_TOTAL_VALUES = 512


@dataclass(frozen=True, slots=True)
class RepairFeedback:
    validation_codes: tuple[str, ...]
    parse_errors: tuple[dict[str, object], ...] = ()
    plan_issues: tuple[dict[str, object], ...] = ()
    guard_issues: tuple[dict[str, object], ...] = ()


class RepairablePlanningFailure(Exception):
    def __init__(self, error: PlanningError, feedback: RepairFeedback) -> None:
        self.error = error
        self.feedback = feedback
        super().__init__(error.message)


def output_hash(result: object) -> str | None:
    if not isinstance(result, GenerateResult) or result.output is None:
        return None
    payload = result.output.model_dump(mode="json")["data"]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def safe_plan_issue(issue: PlanValidationIssue) -> dict[str, object]:
    payload: dict[str, object] = {"code": issue.code.value}
    for field_name in ("node_id", "field", "reference"):
        value = getattr(issue, field_name)
        if value is not None:
            payload[field_name] = _truncate_string(value)
    if issue.edge_index is not None:
        payload["edge_index"] = issue.edge_index
    return payload


def bounded_location_part(value: object) -> str | int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _truncate_string(str(value))


@dataclass(slots=True)
class _RepairProjectionBudget:
    remaining_values: int = _MAX_REPAIR_TOTAL_VALUES


def bounded_repair_value(
    value: object,
    *,
    _depth: int = 0,
    _budget: _RepairProjectionBudget | None = None,
) -> object:
    budget = _budget or _RepairProjectionBudget()
    if budget.remaining_values <= 0:
        return "<truncated:total-values>"
    budget.remaining_values -= 1
    if _depth >= _MAX_REPAIR_JSON_DEPTH:
        return "<truncated:max-depth>"
    if isinstance(value, str):
        return _truncate_string(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        items = sorted(value.items(), key=lambda item: str(item[0]))
        for key, item in items[:_MAX_REPAIR_COLLECTION_ITEMS]:
            projected[_truncate_string(str(key))] = bounded_repair_value(
                item,
                _depth=_depth + 1,
                _budget=budget,
            )
        if len(items) > _MAX_REPAIR_COLLECTION_ITEMS:
            projected["$harness_truncated_items"] = len(items) - _MAX_REPAIR_COLLECTION_ITEMS
        return projected
    if isinstance(value, tuple | list):
        projected_items = [
            bounded_repair_value(
                item,
                _depth=_depth + 1,
                _budget=budget,
            )
            for item in value[:_MAX_REPAIR_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_REPAIR_COLLECTION_ITEMS:
            projected_items.append(
                {"$harness_truncated_items": len(value) - _MAX_REPAIR_COLLECTION_ITEMS}
            )
        return projected_items
    return f"<unsupported:{type(value).__name__}>"


def _truncate_string(value: str) -> str:
    if len(value) <= _MAX_REPAIR_STRING_LENGTH:
        return value
    return f"{value[:_MAX_REPAIR_STRING_LENGTH]}<truncated>"
