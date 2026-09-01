"""Context item、Snapshot 与 Projection 的确定性 JSON 编码和 Hash。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from harness_contracts import ContextItem


def canonical_json(value: object) -> str:
    return json.dumps(
        _thaw(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def context_item_facts(item: ContextItem) -> dict[str, object]:
    """返回稳定来源事实，排除本次收集的 wall-clock 时间。"""

    payload = item.model_dump(mode="json")
    payload.pop("created_at", None)
    freshness = dict(payload["freshness"])
    freshness.pop("observed_at", None)
    payload["freshness"] = freshness
    return payload


def context_item_char_count(item: ContextItem) -> int:
    return len(canonical_json(item.model_dump(mode="json")["content"]))


def stable_item_id(*, source_kind: str, source_id: str, source_version: str, kind: str) -> str:
    digest = canonical_hash(
        {
            "kind": kind,
            "source_id": source_id,
            "source_kind": source_kind,
            "source_version": source_version,
        }
    )
    return f"ctx-{digest}"


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list | frozenset | set):
        items = [_thaw(item) for item in value]
        return sorted(items, key=canonical_json) if isinstance(value, frozenset | set) else items
    return value
