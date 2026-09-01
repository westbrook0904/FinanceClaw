"""Exploration Profile、Action 与 Result 的稳定 canonical facts。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from harness_contracts import (
    ActionProposal,
    ExplorationProfile,
    ExplorationProfileSnapshot,
    RequestInput,
    ResultEnvelope,
)


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


def profile_facts(
    profile: ExplorationProfile | ExplorationProfileSnapshot,
) -> dict[str, object]:
    if isinstance(profile, ExplorationProfile):
        budget = profile.default_budget
    elif isinstance(profile, ExplorationProfileSnapshot):
        budget = profile.budget
    else:
        raise TypeError("profile must be ExplorationProfile or ExplorationProfileSnapshot")
    return {
        "allowed_capability_ids": sorted(profile.allowed_capability_ids),
        "budget": budget.model_dump(mode="json"),
        "memory_required": profile.memory_required,
        "model_capability_id": profile.model_capability_id,
        "profile_id": profile.profile_id,
        "prompt_version": profile.prompt_version,
    }


def exploration_profile_hash(
    profile: ExplorationProfile | ExplorationProfileSnapshot,
) -> str:
    return canonical_hash(profile_facts(profile))


def exploration_scope_hash(profile: ExplorationProfileSnapshot) -> str:
    if not isinstance(profile, ExplorationProfileSnapshot):
        raise TypeError("profile must be ExplorationProfileSnapshot")
    return canonical_hash({"allowed_capability_ids": sorted(profile.allowed_capability_ids)})


def action_proposal_facts(proposal: ActionProposal) -> dict[str, object]:
    if not isinstance(proposal, ActionProposal):
        raise TypeError("proposal must be ActionProposal")
    return {
        "action_id": proposal.action_id,
        "capability_id": proposal.capability_id,
        "catalog_snapshot_hash": proposal.catalog_snapshot_hash,
        "context_projection_hash": proposal.context_projection_hash,
        "exploration_id": proposal.exploration_id,
        "input": proposal.input.model_dump(mode="json"),
        "reason_code": proposal.reason_code,
        "scope_hash": proposal.scope_hash,
        "step": proposal.step,
    }


def action_proposal_hash(proposal: ActionProposal) -> str:
    return canonical_hash(action_proposal_facts(proposal))


def result_envelope_hash(result: ResultEnvelope) -> str:
    if not isinstance(result, ResultEnvelope):
        raise TypeError("result must be ResultEnvelope")
    return canonical_hash(result.model_dump(mode="json"))


def action_fingerprint(capability_id: str, input_value: RequestInput) -> str:
    if not isinstance(capability_id, str) or not capability_id.strip():
        raise TypeError("capability_id must be a non-empty string")
    if not isinstance(input_value, RequestInput):
        raise TypeError("input_value must be RequestInput")
    payload = capability_id + canonical_json(input_value.model_dump(mode="json"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list | frozenset | set):
        items = [_thaw(item) for item in value]
        return sorted(items, key=canonical_json) if isinstance(value, frozenset | set) else items
    return value
