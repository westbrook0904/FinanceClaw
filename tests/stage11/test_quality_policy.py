"""Permanent bilingual admission corpus; model semantic evaluation remains a separate gate."""

import json
from pathlib import Path

import pytest

from financeclaw.shared.memory.policies import explicit_preferences

CASES = json.loads((Path(__file__).parent / "fixtures" / "memory-quality.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_only_explicit_persistent_preferences_commit_without_model(case):
    """Reject temporary, quoted, financial, hypothetical and contradictory auto writes."""
    actual = {key.value: value for key, value in explicit_preferences(case["text"]).items()}
    assert actual == case["expected_auto_preferences"]
