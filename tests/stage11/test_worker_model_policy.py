"""Registered capacities and source-level model policy are independent safety boundaries."""

import pytest

from financeclaw.kernel.context import DataClassification
from financeclaw.memory_worker.model import validate_model_sources
from financeclaw.shared.llm.memory_profiles import memory_model_profiles, memory_profile_fingerprint
from financeclaw.shared.memory.models import MemoryPermissionError
from tests.stage11.test_worker_pipeline import pipeline as pipeline_fixture

pipeline = pipeline_fixture


def test_explicit_memory_alias_uses_its_own_capacity(tmp_path):
    """提取和整理分别绑定别名，不从摘要或默认模型继承错误容量。"""
    from tests.stage1.test_model_configuration import CONFIG, configured

    content = CONFIG.replace(
        "context_window_tokens = 131072",
        "context_window_tokens = 64000\nmax_input_tokens = 32000",
        1,
    )
    content = content.replace(
        "context_window_tokens = 131072", "context_window_tokens = 128000\nmax_input_tokens = 96000"
    )
    content += '\n[tasks]\nmemory_extraction = "root-main"\nmemory_consolidation = "child-main"\n'
    extraction, consolidation = memory_model_profiles(configured(tmp_path, content))
    assert extraction.context_window_tokens == 64000 and extraction.max_input_tokens == 32000
    assert consolidation.context_window_tokens == 128000 and consolidation.max_input_tokens == 96000
    assert extraction.max_tokens == 2000 and consolidation.max_tokens == 4000


def test_provider_change_or_policy_change_invalidates_frozen_memory_profile(tmp_path):
    """端点及处理权限改变后，不允许静默消费原发布身份的记忆任务。"""
    from tests.stage1.test_model_configuration import CONFIG, configured

    first = configured(tmp_path)
    before = memory_profile_fingerprint(memory_model_profiles(first)[0])
    second = configured(tmp_path, CONFIG.replace("child.example", "changed.example"))
    restricted = first.model_copy(
        update={"memory_model_allowed_data_classes": frozenset({DataClassification.PUBLIC})}
    )
    fingerprints = {
        before,
        *(
            memory_profile_fingerprint(memory_model_profiles(settings)[0])
            for settings in (second, restricted)
        ),
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
