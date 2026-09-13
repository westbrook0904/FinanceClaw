"""供应商与别名配置使用真实装配路径，验证发布隔离和运行连接，不请求远程模型。"""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from financeclaw.agent_server.bootstrap import build_components
from financeclaw.kernel.models import ModelProfileRef
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.security.egress import EgressDenied
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.llm.configuration import ModelConfiguration, ProviderDeclaration
from financeclaw.shared.llm.factory import ModelFactory
from financeclaw.shared.llm.memory_profiles import memory_model_profiles
from financeclaw.shared.releases.catalog import build_release_catalogs

CONFIG = """
[defaults]
model = "child-main"
[providers.first]
base_url = "https://root.example/v1"
api_key_env = "TEST_ROOT_MODEL_KEY"
[providers.second]
base_url = "https://child.example/v1"
api_key_env = "TEST_CHILD_MODEL_KEY"
[models.root-main]
provider = "first"
model = "openai:synthetic-root"
max_tokens = 8192
temperature = 0.4
timeout_seconds = 75
context_window_tokens = 131072
[models.child-main]
provider = "second"
model = "openai:deepseek-synthetic-child"
context_window_tokens = 131072
[agents]
finance_agent = "root-main"
ziwei_doushu_agent = "child-main"
"""


def configured(tmp_path, content=CONFIG, **overrides):
    """构造独立配置文件和 Settings，不读取真实凭据。"""
    path = tmp_path / "models.toml"
    path.write_text(content)
    return FinanceClawSettings(
        _env_file=None,
        environment="test",
        debug_full_io=False,
        model_config_path=str(path),
        **{
            "egress_allowed_hosts": frozenset(
                {"api.deepseek.com", "root.example", "child.example"}
            ),
            **overrides,
        },
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("openai:synthetic-root", "openai:new-root"),
        ("root.example", "another-root.example"),
        ("temperature = 0.4", "temperature = 0.8"),
        ("max_tokens = 8192", "max_tokens = 4096"),
        ("timeout_seconds = 75", "timeout_seconds = 120"),
        ('finance_agent = "root-main"', 'finance_agent = "child-main"'),
    ],
)
def test_root_changes_preserve_child_profiles_and_manifests(tmp_path, before, after):
    """只修改根配置，子档案及其模型依赖声明完全一致，后台记忆和摘要也不变。"""
    ziwei_options = {
        "ziwei_enabled": True,
        "ziwei_convention": "x-iztro-civil-candidate@1.0.0",
        "ziwei_hmac_key": "synthetic-ziwei-test-key-01234567890123456789",
        "langsmith_hide_inputs": True,
        "langsmith_hide_outputs": True,
    }
    first = configured(tmp_path, **ziwei_options)
    catalogs = build_release_catalogs(first)
    second = configured(tmp_path, CONFIG.replace(before, after), **ziwei_options)
    changed = build_release_catalogs(second)
    root_before = catalogs.agent_profiles.resolve("finance_agent")
    root_after = changed.agent_profiles.resolve("finance_agent")
    assert root_before != root_after
    assert root_before.worker_manifest == root_after.worker_manifest
    child_manifest = next(
        json.loads(item)
        for item in root_before.worker_manifest
        if json.loads(item)["target_id"] == "ziwei_doushu_agent"
    )
    assert [model["profile_id"] for model in child_manifest["models"]] == ["child-main"]
    for agent in ("ziwei_doushu_agent", "market_research_agent"):
        assert catalogs.agent_profiles.resolve(agent) == changed.agent_profiles.resolve(agent)
    summary = ModelProfileRef(profile_id="child-main", version="1.0.0")
    assert catalogs.model_profiles.resolve(summary) == changed.model_profiles.resolve(summary)
    assert memory_model_profiles(first) == memory_model_profiles(second)


def test_real_factory_routes_credentials_and_cross_provider_fallback(tmp_path, monkeypatch):
    """通过实际 Agent Server 装配验证两个客户端参数和跨服务商降级，API 声明一致。"""
    monkeypatch.setenv("TEST_ROOT_MODEL_KEY", "synthetic-root-key")
    monkeypatch.setenv("TEST_CHILD_MODEL_KEY", "synthetic-child-key")
    content = CONFIG.replace(
        'model = "openai:synthetic-root"',
        'model = "openai:synthetic-root"\nfallbacks = ["child-main"]',
    )
    settings = configured(tmp_path, content)
    components = build_components(settings, enable_persistence=False)
    root = components.default_agent_profile
    ziwei = components.agent_profiles.resolve("ziwei_doushu_agent")
    main = components.model_factory.create(root.model_profile)
    child = components.model_factory.create(ziwei.model_profile)
    assert main.openai_api_base == "https://root.example/v1"
    assert main.openai_api_key.get_secret_value() == "synthetic-root-key"
    assert main.max_tokens == 8192 and main.temperature == 0.4 and main.request_timeout == 75
    assert child.openai_api_base == "https://child.example/v1"
    assert child.openai_api_key.get_secret_value() == "synthetic-child-key"
    assert child.extra_body == {"thinking": {"type": "disabled"}}
    fallback = components.model_factory.fallback_models(
        components.model_profiles.resolve(root.model_profile)
    )[0]
    assert fallback.openai_api_base == child.openai_api_base
    assert fallback.openai_api_key == child.openai_api_key
    assert root == build_release_catalogs(settings).agent_profiles.resolve("finance_agent")
    monkeypatch.setenv("TEST_ROOT_MODEL_KEY", "synthetic-rotated-key")
    assert root == build_release_catalogs(configured(tmp_path, content)).agent_profiles.resolve(
        "finance_agent"
    )
    assert "synthetic-root-key" not in json.dumps(main.metadata)
    assert "synthetic-child-key" not in root.model_dump_json()


def test_missing_connection_never_uses_default_key(tmp_path):
    """新供应商缺失时阻止创建，不能借用旧服务商的地址和密钥。"""
    catalogs = build_release_catalogs(configured(tmp_path))
    factory = ModelFactory(
        catalogs.model_profiles, api_key=SecretStr("wrong"), base_url="https://wrong.example"
    )
    with pytest.raises(ValueError, match="model connection is not configured: first"):
        factory.create(catalogs.agent_profiles.resolve("finance_agent").model_profile)


def test_secret_resolution_supports_dotenv_and_environment_priority(tmp_path, monkeypatch):
    """密钥环境变量名可自行命名，dotenv 可用，进程环境优先且缺失时报错。"""
    monkeypatch.delenv("TEST_ALIAS_KEY", raising=False)
    provider = ProviderDeclaration(base_url="https://example.com/v1", api_key_env="TEST_ALIAS_KEY")
    with pytest.raises(ValidationError, match="TEST_ALIAS_KEY"):
        provider.secret()
    dotenv = tmp_path / ".env"
    dotenv.write_text("TEST_ALIAS_KEY=synthetic-file-key\nUNRELATED=ignored\n")
    assert provider.secret(env_file=dotenv).get_secret_value() == "synthetic-file-key"
    monkeypatch.setenv("TEST_ALIAS_KEY", "synthetic-env-key")
    assert provider.secret(env_file=dotenv).get_secret_value() == "synthetic-env-key"
    monkeypatch.setenv("TEST_ALIAS_KEY", " ")
    with pytest.raises(ValueError, match="empty provider credential"):
        provider.secret(env_file=dotenv)


@pytest.mark.parametrize(
    "before,after,message",
    [
        ('provider = "first"', 'provider = "missing"', "unknown provider alias"),
        ('finance_agent = "root-main"', 'finance_agent = "missing"', "unknown model alias"),
        (
            'finance_agent = "root-main"',
            'misspelled_agent = "root-main"',
            "unknown Agent model bindings",
        ),
        ("temperature = 0.4", "temperatur = 0.4", "Extra inputs"),
        ("context_window_tokens = 131072", "", "context_window_tokens"),
        (
            "context_window_tokens = 131072",
            "context_window_tokens = 16384",
            "insufficient context budget",
        ),
        ("max_tokens = 8192", "max_tokens = 65536", "context output reserve"),
        (
            'model = "openai:synthetic-root"',
            'model = "native:synthetic-root"',
            "String should match",
        ),
        (
            'model = "openai:synthetic-root"',
            'model = "openai:synthetic-root"\nfallbacks = ["missing"]',
            "unknown model alias",
        ),
        (
            'model = "openai:synthetic-root"',
            'model = "openai:synthetic-root"\nfallbacks = ["root-main"]',
            "cyclic model fallbacks",
        ),
        ("https://root.example/v1", "https://name:secret@root.example/v1", "without credentials"),
    ],
)
def test_invalid_declarations_fail_closed(tmp_path, before, after, message):
    """拼写、引用、容量、协议和环形降级错误均明确失败，不能静默改用默认模型。"""
    with pytest.raises(ValueError, match=message):
        build_release_catalogs(configured(tmp_path, CONFIG.replace(before, after)))


def test_active_provider_egress_is_validated(tmp_path):
    """模型配置新增的出站地址仍遵守共享 allowlist。"""
    with pytest.raises(EgressDenied, match="root.example|child.example"):
        build_resources(configured(tmp_path, egress_allowed_hosts=frozenset({"api.deepseek.com"})))


def test_inventory_and_key_rotation_do_not_change_releases(tmp_path, monkeypatch):
    """只登记备用模型不要求其密钥，也不影响任一 Agent 的发布身份。"""
    monkeypatch.setenv("TEST_ROOT_MODEL_KEY", "synthetic-root-key")
    monkeypatch.setenv("TEST_CHILD_MODEL_KEY", "synthetic-child-key")
    before = build_release_catalogs(configured(tmp_path))
    inventory = """
[providers.inventory]
base_url = "https://unused.example/v1"
api_key_env = "MISSING_UNUSED_PROVIDER_KEY"
[models.unused]
provider = "inventory"
model = "openai:unused"
context_window_tokens = 131072
"""
    settings = configured(tmp_path, CONFIG + inventory)
    after = build_components(settings, enable_persistence=False)
    for name in ("finance_agent", "ziwei_doushu_agent", "market_research_agent"):
        assert before.agent_profiles.resolve(name) == after.agent_profiles.resolve(name)
    assert set(settings.model_configuration.active_providers()) == {"first", "second"}


def test_nested_fallbacks_are_ordered_and_cycles_rejected(tmp_path):
    """嵌套备选顺序稳定，跨节点环形引用无法进入运行时。"""
    content = CONFIG.replace(
        'model = "openai:synthetic-root"',
        'model = "openai:synthetic-root"\nfallbacks = ["child-main", "last"]',
    )
    content = content.replace(
        'model = "openai:deepseek-synthetic-child"',
        'model = "openai:deepseek-synthetic-child"\nfallbacks = ["last"]',
    )
    content += """
[models.last]
provider = "second"
model = "openai:last"
context_window_tokens = 131072
"""
    configuration = configured(tmp_path, content).model_configuration
    assert configuration.fallback_aliases("root-main") == ("child-main", "last")
    with pytest.raises(ValueError, match="cyclic model fallbacks"):
        _ = configured(tmp_path, content + 'fallbacks = ["root-main"]\n').model_configuration


def test_unconfigured_agents_keep_default_and_file_is_frozen_per_settings(tmp_path):
    """配置未启用时保持默认模型；启用后同进程声明不会随文件改写而漂移。"""
    settings = FinanceClawSettings(_env_file=None, environment="test")
    root = build_release_catalogs(settings).agent_profiles.resolve("finance_agent")
    assert root.model_profile.profile_id == "default"
    first = configured(tmp_path)
    catalogs = build_release_catalogs(first)
    Path(first.model_config_path).write_text(CONFIG.replace("root.example", "changed.example"))
    assert build_release_catalogs(first).agent_profiles.resolve("finance_agent") == (
        catalogs.agent_profiles.resolve("finance_agent")
    )
    assert (
        catalogs.agent_profiles.resolve("market_research_agent").model_profile.profile_id
        == "child-main"
    )


def test_repository_configuration_is_valid_and_env_path_is_supported(monkeypatch):
    """随镜像提供的 TOML 示例能加载，环境变量只负责定位文件。"""
    path = Path(__file__).resolve().parents[2] / "config/models.toml"
    monkeypatch.setenv("FINANCECLAW_MODEL_CONFIG_PATH", str(path))
    settings = FinanceClawSettings(_env_file=None, environment="test")
    assert settings.model_configuration == ModelConfiguration.from_file(str(path))
    assert (
        build_release_catalogs(settings)
        .agent_profiles.resolve("ziwei_doushu_agent")
        .model_profile.profile_id
        == "ziwei-main"
    )


def test_default_binding_covers_every_agent_and_background_task(tmp_path):
    """只写默认别名即可覆盖全部入口，默认大模型不会扩大记忆来源输出授权。"""
    content = CONFIG.split("[agents]")[0]
    settings = configured(tmp_path, content)
    configuration = settings.model_configuration
    catalogs = build_release_catalogs(settings)
    assert {p.model_profile.profile_id for p in catalogs.agent_profiles.values()} == {"child-main"}
    assert {
        configuration.task_ref(purpose).profile_id
        for purpose in ("summary", "memory_extraction", "memory_consolidation")
    } == {"child-main"}
    extraction, consolidation = memory_model_profiles(settings)
    assert extraction.model == consolidation.model == "openai:deepseek-synthetic-child"
    assert extraction.max_tokens == 2000 and consolidation.max_tokens == 4000
    changed = configured(tmp_path, content.replace('model = "child-main"', 'model = "root-main"'))
    assert (
        build_release_catalogs(changed)
        .agent_profiles.resolve("market_research_agent")
        .model_profile.profile_id
        == "root-main"
    )
    assert memory_model_profiles(changed)[0].model == "openai:synthetic-root"


@pytest.mark.parametrize(
    "extra,message",
    [
        ('\n[tasks]\nsummary = "missing"\n', "unknown model alias"),
        ('\n[tasks]\nmemory_extraction = "missing"\n', "unknown model alias"),
        ('\n[tasks]\nmemory_consolidation = "missing"\n', "unknown model alias"),
        ('\n[tasks]\nsummarry = "child-main"\n', "Extra inputs"),
    ],
)
def test_background_overrides_do_not_hide_configuration_errors(tmp_path, extra, message):
    """默认值仅处理省略，不能掩盖显式填写的错误。"""
    with pytest.raises(ValueError, match=message):
        build_release_catalogs(configured(tmp_path, CONFIG + extra))


def test_missing_or_invalid_default_fails_without_env_fallback(tmp_path):
    """缺少默认声明或默认别名无效时，不从旧 MODEL 环境变量猜测。"""
    for content in (
        CONFIG.replace('[defaults]\nmodel = "child-main"', ""),
        CONFIG.replace('[defaults]\nmodel = "child-main"', '[defaults]\nmodel = "missing"'),
    ):
        with pytest.raises(ValueError):
            build_release_catalogs(configured(tmp_path, content))


def test_old_model_environment_variables_no_longer_affect_toml(tmp_path, monkeypatch):
    """冲突的旧变量不会改写新配置的地址、模型或容量；密钥仍按引用的变量读取。"""
    settings = configured(tmp_path)
    before = build_release_catalogs(settings).agent_profiles.resolve("finance_agent")
    for key, value in {
        "MODEL": "openai:must-not-be-used",
        "PROVIDER_BASE_URL": "https://must-not-be-used.example/v1",
        "MODEL_MAX_TOKENS": "999999999",
        "SUMMARY_MODEL": "openai:must-not-be-used",
        "MEMORY_EXTRACTION_MODEL": "openai:must-not-be-used",
        "MEMORY_CONSOLIDATION_MODEL": "openai:must-not-be-used",
    }.items():
        monkeypatch.setenv(f"FINANCECLAW_{key}", value)
    after = configured(tmp_path)
    assert before == build_release_catalogs(after).agent_profiles.resolve("finance_agent")
    assert memory_model_profiles(settings) == memory_model_profiles(after)
    assert "provider_base_url" not in FinanceClawSettings.model_fields
    assert "model" not in FinanceClawSettings.model_fields


def test_provider_credentials_respect_settings_dotenv_override(tmp_path, monkeypatch):
    """运行期连接使用本 Settings 的凭据文件，显式关闭 dotenv 后不会读回其他密钥。"""
    from financeclaw.shared.llm.factory import configured_connections

    settings = configured(tmp_path)
    monkeypatch.delenv("TEST_ROOT_MODEL_KEY", raising=False)
    dotenv = tmp_path / ".env.custom"
    dotenv.write_text("TEST_ROOT_MODEL_KEY=synthetic-custom-file-key\n")
    explicit = FinanceClawSettings(_env_file=dotenv, model_config_path=settings.model_config_path)
    ref = explicit.model_configuration.ref("finance_agent")
    assert configured_connections(explicit, [ref])["first"].api_key.get_secret_value() == (
        "synthetic-custom-file-key"
    )
    with pytest.raises(ValidationError, match="TEST_ROOT_MODEL_KEY"):
        configured_connections(settings, [ref])


def test_summary_and_memory_workers_use_only_their_own_provider_credentials(tmp_path, monkeypatch):
    """真实装配四个供应商；图进程不需要记忆密钥，记忆进程也不需要根/摘要密钥。"""
    from financeclaw.memory_worker.bootstrap import build_memory_worker

    content = (
        CONFIG
        + """
[providers.summary-only]
base_url = "https://summary.example/v1"
api_key_env = "TEST_SUMMARY_KEY"
[models.summary-alias]
provider = "summary-only"
model = "openai:summary-only"
max_tokens = 4096
context_window_tokens = 131072
[providers.memory-only]
base_url = "https://memory.example/v1"
api_key_env = "TEST_MEMORY_KEY"
[models.memory-alias]
provider = "memory-only"
model = "openai:memory-only"
max_tokens = 1024
context_window_tokens = 8192
timeout_seconds = 60
[tasks]
summary = "summary-alias"
memory_extraction = "child-main"
memory_consolidation = "memory-alias"
"""
    )
    monkeypatch.setenv("TEST_ROOT_MODEL_KEY", "synthetic-root-key")
    monkeypatch.setenv("TEST_CHILD_MODEL_KEY", "synthetic-child-key")
    monkeypatch.setenv("TEST_SUMMARY_KEY", "synthetic-summary-key")
    monkeypatch.delenv("TEST_MEMORY_KEY", raising=False)
    settings = configured(
        tmp_path,
        content,
        egress_allowed_hosts=frozenset({"root.example", "child.example", "summary.example"}),
    )
    components = build_components(settings, enable_persistence=False)
    summary = components.model_factory.create(components.agent_factory.summary_profile)
    assert summary.openai_api_base == "https://summary.example/v1"
    assert summary.openai_api_key.get_secret_value() == "synthetic-summary-key"
    expected_memory_profiles = memory_model_profiles(settings)
    monkeypatch.delenv("TEST_ROOT_MODEL_KEY")
    monkeypatch.delenv("TEST_SUMMARY_KEY")
    monkeypatch.setenv("TEST_MEMORY_KEY", "synthetic-memory-key")
    created = []
    original_create = ModelFactory.create

    def capture_create(factory, ref):
        """记录真实客户端的连接参数，不向远程供应商发送请求。"""
        model = original_create(factory, ref)
        created.append(model)
        return model

    monkeypatch.setattr(ModelFactory, "create", capture_create)
    worker_settings = configured(
        tmp_path,
        content,
        process_role="memory_worker",
        database_url=f"sqlite:///{tmp_path}/memory.db",
        database_auto_create_schema=True,
        egress_allowed_hosts=frozenset({"child.example", "memory.example"}),
    )
    worker = build_memory_worker(worker_settings)
    try:
        assert [model.openai_api_base for model in created] == [
            "https://child.example/v1",
            "https://memory.example/v1",
        ]
        assert [model.openai_api_key.get_secret_value() for model in created] == [
            "synthetic-child-key",
            "synthetic-memory-key",
        ]
        assert (
            worker.extraction.handler.model.profile,
            worker.consolidation.handler.model.profile,
        ) == expected_memory_profiles
        assert worker.consolidation.handler.model.timeout_seconds == 60
        assert worker.consolidation.handler.model.profile.max_tokens == 1024
    finally:
        worker.database.close()
