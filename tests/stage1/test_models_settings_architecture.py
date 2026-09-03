"""`test_models_settings_architecture` 模块提供`stage1`相关能力。"""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.llm import (
    ModelFactory,
    ModelProfile,
    ModelProfileCatalog,
    ModelProfileRef,
)

ROOT = Path(__file__).resolve().parents[2]


def test_deepseek_openai_compatible_configuration_is_explicit() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    settings = FinanceClawSettings(
        environment="test",
        model="openai:deepseek-v4-pro",
        provider_base_url="https://api.deepseek.com",
        provider_api_key=SecretStr("test-placeholder"),
        debug_full_io=False,
    )
    components = build_components(settings)
    model = components.model_factory.create(ModelProfileRef(profile_id="default", version="1.0.0"))

    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "deepseek-v4-pro"
    assert model.openai_api_base == "https://api.deepseek.com"
    assert type(model.openai_api_key).__name__ == "SecretStr"


def test_production_rejects_debug_or_missing_oidc_authentication() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    with pytest.raises(ValidationError, match="debug_full_io"):
        FinanceClawSettings(environment="production", offline_model=False, debug_full_io=True)
    with pytest.raises(ValidationError, match="oidc_issuer"):
        FinanceClawSettings(environment="production", offline_model=False, debug_full_io=False)


def test_model_fallback_governance_rejects_capability_downgrade() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 primary，供后续步骤使用。
    primary = ModelProfile(
        profile_id="primary",
        version="1.0.0",
        model="openai:primary",
        fallback_profiles=(ModelProfileRef(profile_id="fallback", version="1.0.0"),),
    )
    # 准备 fallback，供后续步骤使用。
    fallback = ModelProfile(
        profile_id="fallback",
        version="1.0.0",
        model="openai:fallback",
        supports_tool_calling=False,
    )
    # 准备 factory，供后续步骤使用。
    factory = ModelFactory(
        ModelProfileCatalog((primary, fallback)),
        api_key=SecretStr("test-placeholder"),
        base_url="https://example.invalid",
    )

    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ValueError, match="tool calling"):
        factory.fallback_models(primary)


def test_production_dependency_graph_has_no_stage1_legacy_runtime() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 pyproject，供后续步骤使用。
    pyproject = (ROOT / "pyproject.toml").read_text()
    # 准备 config，供后续步骤使用。
    config = json.loads((ROOT / "langgraph.json").read_text())
    # 准备 source，供后续步骤使用。
    source = "\n".join(path.read_text() for path in (ROOT / "financeclaw").rglob("*.py"))
    # 准备 removed，供后续步骤使用。
    removed = (
        "harness_runtime",
        "harness_registry",
        "harness_selection",
        "harness_spi",
        "harness_plugin_local",
        "harness_contracts",
        "harness_events",
        "harness_trace",
    )

    # 继续执行前验证内部不变量。
    assert all(name not in source for name in removed)
    # 继续执行前验证内部不变量。
    assert all(name not in pyproject for name in removed)
    # 继续执行前验证内部不变量。
    assert config["graphs"] == {
        "finance_agent": "./financeclaw/orchestration/graphs/server_graphs.py:finance_agent",
        "market_research_agent": (
            "./financeclaw/orchestration/graphs/server_graphs.py:market_research_agent"
        ),
        "direct_tool": "./financeclaw/orchestration/graphs/server_graphs.py:direct_tool",
        "portfolio_review_v1": (
            "./financeclaw/orchestration/graphs/server_graphs.py:portfolio_review_v1"
        ),
    }
