import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.models import ModelFactory, ModelProfile, ModelProfileCatalog, ModelProfileRef

ROOT = Path(__file__).resolve().parents[2]


def test_deepseek_openai_compatible_configuration_is_explicit() -> None:
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
    with pytest.raises(ValidationError, match="debug_full_io"):
        FinanceClawSettings(environment="production", offline_model=False, debug_full_io=True)
    with pytest.raises(ValidationError, match="oidc_issuer"):
        FinanceClawSettings(environment="production", offline_model=False, debug_full_io=False)


def test_model_fallback_governance_rejects_capability_downgrade() -> None:
    primary = ModelProfile(
        profile_id="primary",
        version="1.0.0",
        model="openai:primary",
        fallback_profiles=(ModelProfileRef(profile_id="fallback", version="1.0.0"),),
    )
    fallback = ModelProfile(
        profile_id="fallback",
        version="1.0.0",
        model="openai:fallback",
        supports_tool_calling=False,
    )
    factory = ModelFactory(
        ModelProfileCatalog((primary, fallback)),
        api_key=SecretStr("test-placeholder"),
        base_url="https://example.invalid",
    )

    with pytest.raises(ValueError, match="tool calling"):
        factory.fallback_models(primary)


def test_production_dependency_graph_has_no_stage1_legacy_runtime() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    config = json.loads((ROOT / "langgraph.json").read_text())
    source = "\n".join(path.read_text() for path in (ROOT / "financeclaw").rglob("*.py"))
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

    assert all(name not in source for name in removed)
    assert all(name not in pyproject for name in removed)
    assert config["graphs"] == {
        "finance_agent": "./financeclaw/graphs/server_graphs.py:finance_agent",
        "market_research_agent": ("./financeclaw/graphs/server_graphs.py:market_research_agent"),
        "direct_tool": "./financeclaw/graphs/server_graphs.py:direct_tool",
        "portfolio_review_v1": ("./financeclaw/graphs/server_graphs.py:portfolio_review_v1"),
    }
