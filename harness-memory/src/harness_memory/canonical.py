"""Memory facts 的 canonical JSON、hash、size 与稳定排序。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from harness_contracts import MemoryQuery, MemoryRecord, MemoryWriteProposal


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


def canonical_size(value: object) -> int:
    return len(canonical_json(value).encode("utf-8"))


def proposal_facts(proposal: MemoryWriteProposal) -> dict[str, object]:
    return {
        "content": proposal.model_dump(mode="json")["content"],
        "evidence_refs": list(proposal.evidence_refs),
        "expires_at": (
            proposal.expires_at.isoformat() if proposal.expires_at is not None else None
        ),
        "kind": proposal.kind.value,
        "namespace": proposal.namespace,
        "proposal_id": proposal.proposal_id,
        "provenance": proposal.provenance.model_dump(mode="json"),
        "sensitivity": proposal.sensitivity.value,
        "source_fact_hash": proposal.source_fact_hash,
        "subject_id": proposal.subject_id,
        "tags": sorted(proposal.tags),
        "tenant_id": proposal.tenant_id,
    }


def proposal_hash(proposal: MemoryWriteProposal) -> str:
    return canonical_hash(proposal_facts(proposal))


def query_hash(query: MemoryQuery) -> str:
    return canonical_hash(
        {
            "kinds": sorted(kind.value for kind in query.kinds),
            "limit": query.limit,
            "namespaces": sorted(query.namespaces),
            "subject_id": query.subject_id,
            "tags": sorted(query.tags),
            "tenant_id": query.tenant_id,
            "text": query.text,
        }
    )


def memory_id_for(proposal: MemoryWriteProposal) -> str:
    digest = canonical_hash(
        {
            "namespace": proposal.namespace,
            "proposal_id": proposal.proposal_id,
            "subject_id": proposal.subject_id,
            "tenant_id": proposal.tenant_id,
        }
    )
    return f"mem-{digest}"


def record_order(record: MemoryRecord) -> tuple[float, str]:
    return (-record.created_at.timestamp(), record.memory_id)


def matches_query(record: MemoryRecord, query: MemoryQuery) -> bool:
    if record.tenant_id != query.tenant_id or record.subject_id != query.subject_id:
        return False
    if record.namespace not in query.namespaces or record.kind not in query.kinds:
        return False
    if not query.tags.issubset(record.tags):
        return False
    if query.text is not None:
        return query.text.casefold() in canonical_json(record.content).casefold()
    return True


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list | frozenset | set):
        items = [_thaw(item) for item in value]
        return sorted(items, key=canonical_json) if isinstance(value, frozenset | set) else items
    return value
