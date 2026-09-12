"""Registered capacities and source-level model policy are independent safety boundaries."""

import pytest

from financeclaw.kernel.context import DataClassification
from financeclaw.memory_worker.model import validate_model_sources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.llm.memory_profiles import memory_model_profiles, memory_profile_fingerprint
from financeclaw.shared.memory.models import MemoryPermissionError
from tests.stage11.test_worker_pipeline import pipeline as pipeline_fixture

pipeline = pipeline_fixture


def test_explicit_primary_memory_model_uses_primary_capacity():
    """Keep the primary capacity when an explicit memory model selects the primary."""
    settings = FinanceClawSettings(
        _env_file=None,
        model="openai:primary",
        summary_model="openai:summary",
        memory_extraction_model="openai:primary",
        model_context_window_tokens=64000,
        model_max_input_tokens=32000,
        summary_context_window_tokens=128000,
        summary_max_input_tokens=96000,
        model_max_tokens=2000,
        context_reserved_output=2000,
        context_tool_schema_reserve=1000,
        context_system_policy_reserve=1000,
        context_safety_margin=256,
    )
    extraction, consolidation = memory_model_profiles(settings)
    assert extraction.context_window_tokens == 64000 and extraction.max_input_tokens == 32000
    assert consolidation.context_window_tokens == 128000 and consolidation.max_input_tokens == 96000


def test_provider_change_or_policy_change_invalidates_frozen_memory_profile():
    """Endpoint, class and region changes cannot silently consume already queued work."""
    first = FinanceClawSettings(_env_file=None, provider_base_url="https://one.example/v1")
    second = first.model_copy(update={"provider_base_url": "https://two.example/v1"})
    restricted = first.model_copy(
        update={"memory_model_allowed_data_classes": frozenset({DataClassification.PUBLIC})}
    )
    fingerprints = {
        memory_profile_fingerprint(memory_model_profiles(settings)[0])
        for settings in (first, second, restricted)
    }
    assert len(fingerprints) == 3


def test_source_classification_region_and_phase_budget_are_enforced(pipeline):
    """Reject a model that cannot handle the original source or its separately frozen phase cap."""
    database, _, model, _, _, actor, source = pipeline
    refs = (source.evidence_ref(),)
    public = model.profile.model_copy(
        update={"allowed_data_classes": frozenset({DataClassification.PUBLIC})}
    )
    other_region = model.profile.model_copy(update={"allowed_regions": frozenset({"other-region"})})
    oversized_output = model.profile.model_copy(update={"max_tokens": 4000})
    with database.session_factory() as session:
        for profile in (public, other_region):
            with pytest.raises(MemoryPermissionError, match="classification or region"):
                validate_model_sources(session, actor, refs, profile)
        with pytest.raises(MemoryPermissionError, match="capacity"):
            validate_model_sources(session, actor, refs, oversized_output, input_tokens=100)
        validate_model_sources(
            session, actor, refs, oversized_output, input_tokens=100, phase="consolidation"
        )
        with pytest.raises(MemoryPermissionError, match="capacity"):
            validate_model_sources(session, actor, refs, model.profile, input_tokens=24001)
