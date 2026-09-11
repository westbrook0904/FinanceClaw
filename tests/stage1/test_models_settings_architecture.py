"""`test_models_settings_architecture` 模块提供`stage1`相关能力。"""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from financeclaw.agent_server.llm.factory import ModelFactory
from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.support import build_components

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("model_name", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_deepseek_openai_compatible_configuration_is_explicit(model_name: str) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    settings = FinanceClawSettings(
        environment="test",
        model=f"openai:{model_name}",
        provider_base_url="https://api.deepseek.com",
        provider_api_key=SecretStr("test-placeholder"),
        debug_full_io=False,
    )
    components = build_components(settings)
    model = components.model_factory.create(ModelProfileRef(profile_id="default", version="1.0.0"))

    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == model_name
    assert model.openai_api_base == "https://api.deepseek.com"
    assert type(model.openai_api_key).__name__ == "SecretStr"
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_production_rejects_debug_or_missing_oidc_authentication() -> None:
    """生产先拒绝完整调试输出，关闭调试后仍必须配置 OIDC 认证。"""
    with pytest.raises(ValidationError) as debug_error:
        FinanceClawSettings(
            _env_file=None, environment="production", offline_model=False, debug_full_io=True
        )
    # 错误回显也含 debug_full_io 字段名，必须验证校验器消息而非整个异常文本。
    assert "debug_full_io must be disabled in production" in debug_error.value.errors()[0]["msg"]
    with pytest.raises(ValidationError) as auth_error:
        FinanceClawSettings(
            _env_file=None, environment="production", offline_model=False, debug_full_io=False
        )
    assert (
        "oidc_issuer, oidc_audience and oidc_jwks_url are required"
        in auth_error.value.errors()[0]["msg"]
    )


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
        "finance_agent_v1_6_0": "./financeclaw/agent_server/graphs/bff_graphs.py:finance_agent",
    }
    local_config = json.loads((ROOT / "langgraph.local.json").read_text())
    assert local_config["graphs"] == config["graphs"]
