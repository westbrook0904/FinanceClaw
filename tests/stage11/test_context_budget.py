"""Stage 11 完整输入容量、输出预留和冻结降级链的回归测试。"""

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from financeclaw.kernel.models import ModelProfile
from financeclaw.shared.llm.budget import ContextBudgetPlanner
from financeclaw.shared.releases.fingerprint import configuration_fingerprint


def profile(**changes):
    """显式构造有独立输入 cap 的小容量模型档案。"""
    return ModelProfile(profile_id="test", version="1.0.0", model="openai:test", **changes)


def test_independent_input_cap_does_not_pay_output_reserve_twice():
    """8k 输入 cap 在20k总窗口内仅扣安全余量，4k输出不再次从8k扣除。"""
    planner = ContextBudgetPlanner(
        profile(context_window_tokens=20_000, max_input_tokens=8000, max_tokens=4000),
        30_000,
        safety_margin=200,
    )
    assert planner.input_limit == 7800


def test_fallback_common_capacity_is_frozen_and_fingerprinted():
    """预处理采用更小fallback窗口，其容量更改同时改变release指纹。"""
    primary = profile(context_window_tokens=32_000, max_tokens=4000)
    smaller = primary.model_copy(update={"profile_id": "smaller", "context_window_tokens": 12_000})
    planner = ContextBudgetPlanner(primary, 40_000, fallback_profiles=(smaller,), safety_margin=100)
    assert planner.input_limit == 7900
    assert configuration_fingerprint(primary, smaller) != configuration_fingerprint(primary)
    assert planner.input_limit < ContextBudgetPlanner(primary, 40_000).input_limit


def test_full_request_schema_and_system_content_share_final_estimator(monkeypatch):
    """系统与Schema超过容量时直接失败；离线估算器版本被明确记录。"""
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")
    planner = ContextBudgetPlanner(profile(context_window_tokens=5000, max_tokens=1000), 6000)
    messages = [SystemMessage(content="policy" * 1000), HumanMessage(content="small user")]
    assert planner.estimator_id == "utf8-bytes-v1"
    with pytest.raises(ValueError, match="input budget"):
        planner.check(messages)
    assert (
        planner.estimate([messages[-1]], output_schema={"description": "schema" * 1000})
        > planner.input_limit
    )
    assert messages[-1].content == "small user"


def test_local_memory_omission_diagnostics_cannot_consume_model_input_capacity():
    """Dropping optional memory must not replace it with a charged local audit payload."""
    planner = ContextBudgetPlanner(profile(context_window_tokens=5000, max_tokens=1000), 6000)
    plain = SystemMessage(content="Mandatory policy")
    observed = plain.model_copy(
        update={
            "additional_kwargs": {
                "financeclaw_memory_omissions": [
                    {"item_id": "memory" * 100, "reason": "token_budget"}
                ]
                * 100,
            }
        }
    )
    assert planner.estimate([plain]) == planner.estimate([observed])
    assert observed.additional_kwargs["financeclaw_memory_omissions"]
