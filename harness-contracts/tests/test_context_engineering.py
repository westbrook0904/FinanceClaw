"""Context Engineering wire contracts。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from harness_contracts import (
    ContextConsumer,
    ContextFreshness,
    ContextItem,
    ContextProjection,
    ContextProvenance,
    ContextSensitivity,
    ContextSourceKind,
    ContextSourceRef,
    ContextTrustTier,
    ContextUseRecord,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def context_item() -> ContextItem:
    return ContextItem(
        item_id="context-item",
        kind="request",
        content={"goal": "compare"},
        source=ContextSourceRef(
            source_kind=ContextSourceKind.REQUEST,
            source_id="request-source",
        ),
        provenance=ContextProvenance(producer="contract-test"),
        freshness=ContextFreshness(source_version="v1", observed_at=NOW),
        trust_tier=ContextTrustTier.USER,
        sensitivity=ContextSensitivity.CONFIDENTIAL,
        created_at=NOW,
    )


class ContextEngineeringContractTests(unittest.TestCase):
    def test_projection_and_use_record_round_trip(self) -> None:
        projection = ContextProjection(
            consumer=ContextConsumer.PLAN,
            snapshot_id="snapshot",
            items=(context_item(),),
            projection_hash="a" * 64,
        )
        use_record = ContextUseRecord(
            use_id="use",
            consumer=ContextConsumer.PLAN,
            snapshot_id="snapshot",
            snapshot_hash="b" * 64,
            projection_hash=projection.projection_hash,
            included_item_ids=("context-item",),
            assembled_at=NOW,
        )

        self.assertEqual(
            ContextProjection.model_validate(projection.model_dump(mode="json")),
            projection,
        )
        self.assertEqual(
            ContextUseRecord.model_validate(use_record.model_dump(mode="json")),
            use_record,
        )

    def test_hashes_and_timestamps_are_strict(self) -> None:
        with self.assertRaises(ValidationError):
            ContextProjection(
                consumer=ContextConsumer.ROUTE,
                snapshot_id="snapshot",
                items=(),
                projection_hash="not-a-sha256",
            )
        with self.assertRaises(ValidationError):
            context_item().model_copy(
                update={
                    "freshness": ContextFreshness(
                        source_version="v1",
                        observed_at=datetime.now(),
                    )
                }
            )


if __name__ == "__main__":
    unittest.main()
