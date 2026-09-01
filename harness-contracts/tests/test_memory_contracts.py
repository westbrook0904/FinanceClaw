"""Agent Foundation F3 Memory 公共契约测试。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from harness_contracts import (
    MemoryKind,
    MemoryProvenance,
    MemoryRecord,
    MemorySensitivity,
    MemoryWriteDraft,
    MemoryWriteProposal,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


class MemoryContractTests(unittest.TestCase):
    def test_draft_is_model_safe_and_rejects_storage_control_fields(self) -> None:
        draft = MemoryWriteDraft(
            kind=MemoryKind.PREFERENCE,
            content={"language": "zh-CN"},
            tags={"locale"},
            evidence_refs=("request:req-1",),
        )

        self.assertEqual(draft.kind, MemoryKind.PREFERENCE)
        with self.assertRaises(ValidationError):
            MemoryWriteDraft.model_validate(
                {
                    **draft.model_dump(mode="json"),
                    "tenant_id": "model-controlled",
                }
            )
        with self.assertRaises(ValidationError):
            MemoryWriteDraft.model_validate(
                {
                    **draft.model_dump(mode="json"),
                    "hidden_reasoning": "must never be persisted",
                }
            )

    def test_record_is_create_only_and_timezone_aware(self) -> None:
        values = {
            "memory_id": "memory-1",
            "tenant_id": "tenant-a",
            "subject_id": "subject-a",
            "namespace": "profile",
            "kind": MemoryKind.DOMAIN_FACT,
            "content": {"risk": "low"},
            "sensitivity": MemorySensitivity.INTERNAL,
            "provenance": MemoryProvenance(
                producer="test",
                source_fact_hash="a" * 64,
                evidence_refs=("request:req-1",),
            ),
            "created_at": NOW,
            "updated_at": NOW,
        }

        record = MemoryRecord(**values)
        self.assertEqual(record.updated_at, record.created_at)
        with self.assertRaises(ValidationError):
            MemoryRecord(**{**values, "updated_at": NOW + timedelta(seconds=1)})
        with self.assertRaises(ValidationError):
            MemoryRecord(
                **{
                    **values,
                    "created_at": NOW.replace(tzinfo=None),
                    "updated_at": NOW.replace(tzinfo=None),
                }
            )

    def test_proposal_requires_aligned_provenance(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryWriteProposal(
                proposal_id="proposal-1",
                proposal_hash="b" * 64,
                tenant_id="tenant-a",
                subject_id="subject-a",
                namespace="profile",
                kind=MemoryKind.PREFERENCE,
                content={"language": "zh-CN"},
                sensitivity=MemorySensitivity.CONFIDENTIAL,
                evidence_refs=("request:req-1",),
                source_fact_hash="a" * 64,
                provenance=MemoryProvenance(
                    producer="test",
                    source_fact_hash="c" * 64,
                    evidence_refs=("request:req-1",),
                ),
            )


if __name__ == "__main__":
    unittest.main()
