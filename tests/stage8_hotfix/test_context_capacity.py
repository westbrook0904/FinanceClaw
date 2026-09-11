"""扩大上下文后验证真实历史装配、预算预留与发布一致性。"""

import pytest
from pydantic import ValidationError

from financeclaw.agent_server.context import budget as context_module
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.catalog import build_release_catalogs


def config(tmp_path, **changes):
    """独立数据库与离线模型，不访问本机凭证或真实模型。"""
    return FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'context.db'}",
        database_auto_create_schema=True,
        artifact_root=str(tmp_path / "artifacts"),
        **changes,
    )


def test_disabled_tokenizer_cache_never_loads_encoding(tmp_path, monkeypatch):
    """显式禁用优先于备用缓存目录，离线初始化不尝试加载或下载编码。"""
    from hashlib import sha1

    source = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    (tmp_path / sha1(source.encode()).hexdigest()).touch()
    monkeypatch.setenv("DATA_GYM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", "")

    def unexpected_load(name):
        """任何编码加载都会违反离线禁用约定。"""
        pytest.fail("disabled cache must not load an encoding")

    monkeypatch.setattr(context_module.tiktoken, "get_encoding", unexpected_load)
    counter = context_module.TokenCounter()
    assert counter.text("中文 abc") == len("中文 abc".encode())
    assert counter.truncate("中文 abc", 5) == "中"


@pytest.mark.parametrize("changes", [{"model_max_tokens": 65_536}, {"context_input_limit": 65_536}])
def test_invalid_generation_or_reserve_budget_is_rejected(tmp_path, changes):
    """不能把最大生成量设得高于预留，或让各预留占满上下文。"""
    with pytest.raises(ValidationError, match="context"):
        config(tmp_path, **changes)


def test_history_window_participates_in_shared_release_fingerprint(tmp_path):
    """调整上下文策略会改变发布指纹，防止两端配置不一致后静默恢复。"""
    settings = config(tmp_path)
    first = build_release_catalogs(settings).agent_profiles.resolve("finance_agent", "1.6.0")
    same = build_release_catalogs(config(tmp_path)).agent_profiles.resolve("finance_agent", "1.6.0")
    changed = build_release_catalogs(
        config(tmp_path, context_recent_turns=2)
    ).agent_profiles.resolve("finance_agent", "1.6.0")
    assert first.configuration_fingerprint == same.configuration_fingerprint
    assert first.configuration_fingerprint != changed.configuration_fingerprint
