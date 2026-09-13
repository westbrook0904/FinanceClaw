"""测试使用固定模型声明和合成凭据，不依赖开发者的 TOML 或本地 .env。"""

from pathlib import Path

import pytest

from financeclaw.shared.infrastructure.settings import FinanceClawSettings


@pytest.fixture(autouse=True)
def isolated_model_configuration(monkeypatch):
    """用真实配置入口注入测试目录；单个测试仍可显式指定其他 TOML。"""
    monkeypatch.setenv(
        "FINANCECLAW_MODEL_CONFIG_PATH", str(Path(__file__).parent / "fixtures/models.toml")
    )
    monkeypatch.setenv("FINANCECLAW_TEST_MODEL_API_KEY", "synthetic-test-model-key")
    monkeypatch.setitem(FinanceClawSettings.model_config, "env_file", None)
