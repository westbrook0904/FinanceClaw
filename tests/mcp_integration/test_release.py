"""配置化 MCP 的显式绑定、发布变化及协调端不联网约束。"""

import json
import subprocess
import sys

import pytest

from financeclaw.agent_server.bootstrap import build_components
from financeclaw.shared.mcp.configuration import MCPConfiguration, MCPRelease
from financeclaw.shared.releases.catalog import build_release_catalogs


def test_api_worker_releases_match_without_credentials(settings):
    """完整发布能离线装配，MCP 不泄露给未绑定的根或紫微 Agent。"""
    declared = build_release_catalogs(settings)
    runtime = build_components(settings)
    for key, profile in declared.agent_profiles.items():
        assert profile == runtime.agent_profiles[key]
    for key, tool in declared.tool_catalog.items():
        assert tool.governance == runtime.tool_catalog[key].governance
    root = declared.agent_profiles.resolve("finance_agent")
    assert "mcp__hotel__search" in {ref.tool_id for ref in root.allowed_tools}
    assert "mcp__other__search" not in {ref.tool_id for ref in root.allowed_tools}
    for name in ("market_research_agent", "ziwei_doushu_agent"):
        assert not any(
            ref.tool_id.startswith("mcp__")
            for ref in declared.agent_profiles.resolve(name).allowed_tools
        )


def test_shared_loader_does_not_import_agent_or_sdk(config_path):
    """API 冷进程加载契约不导入执行实现、不获取凭据、不初始化 SDK。"""
    script = f"""
import sys
from financeclaw.shared.mcp.configuration import MCPRelease
release = MCPRelease({str(config_path)!r}, env_file=None)
assert len(release.entries) == 4
for prefix in ('financeclaw.agent_server', 'langchain_mcp_adapters', 'mcp.client'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_api_boots_with_enabled_mcp_without_sdk_or_credentials(config_path, tmp_path):
    """真实 API lifespan 只需已导入文件，远端和执行端凭据均不参与启动。"""
    script = f"""
import asyncio
import sys
from financeclaw.api.bootstrap import create_default_app
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
settings = FinanceClawSettings(
    _env_file=None, environment='test', offline_model=True, debug_full_io=False,
    mcp_config_path={str(config_path)!r}, database_url={f"sqlite:///{tmp_path}/bff.db"!r},
    database_auto_create_schema=True, artifact_root={str(tmp_path / "artifacts")!r},
)
app = create_default_app(settings, client=object())
async def inspect():
    async with app.router.lifespan_context(app):
        assert app.state.resources.database.ping()
        for prefix in ('financeclaw.agent_server', 'langchain_mcp_adapters', 'mcp.client'):
            assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules)
asyncio.run(inspect())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field", ["schema", "endpoint", "credential_ref", "binding", "policy"])
def test_mcp_changes_freeze_affected_agent_release(settings, config_path, field):
    """参数、端点、凭据身份和策略变化影响使用者，不改变紫微模型发布。"""
    before = build_release_catalogs(settings)
    text = config_path.read_text()
    manifest_path = config_path.parent / "hotel.json"
    raw = json.loads(manifest_path.read_text())
    if field == "schema":
        raw["tools"][0]["inputSchema"]["properties"]["query"]["maxLength"] = 30
    elif field == "endpoint":
        text = text.replace("hotel.example/mcp", "hotel.example/changed")
        raw["endpoint"] = "https://hotel.example/changed"
    elif field == "credential_ref":
        text = text.replace("TEST_MCP_KEY", "TEST_MCP_OTHER_KEY")
    elif field == "binding":
        text = text.replace('"hotel.search", "hotel.detail"', '"other.search"')
    else:
        text = text.replace('["travel:read"]', '["travel:read", "travel:extra"]')
    config_path.write_text(text)
    manifest_path.write_text(json.dumps(raw))
    settings.__dict__.pop("mcp_release")
    after = build_release_catalogs(settings)
    assert (
        before.agent_profiles.resolve("finance_agent").configuration_fingerprint
        != after.agent_profiles.resolve("finance_agent").configuration_fingerprint
    )
    assert before.agent_profiles.resolve("ziwei_doushu_agent") == after.agent_profiles.resolve(
        "ziwei_doushu_agent"
    )


def test_unbound_service_schema_does_not_change_root(settings, config_path):
    """目录里存在但未被任何 Worker 使用的服务，不污染根发布指纹。"""
    before = build_release_catalogs(settings).agent_profiles.resolve("finance_agent")
    path = config_path.parent / "other.json"
    raw = json.loads(path.read_text())
    raw["tools"][0]["description"] = "changed unrelated service"
    path.write_text(json.dumps(raw))
    settings.__dict__.pop("mcp_release")
    after = build_release_catalogs(settings).agent_profiles.resolve("finance_agent")
    assert before == after


def test_disabled_services_need_no_files_or_secrets(config_path):
    """禁用服务可保留绑定，启用前不依赖导入文件和真实密钥。"""
    config_path.write_text(config_path.read_text().replace("enabled = true", "enabled = false"))
    for path in config_path.parent.glob("*.json"):
        path.unlink()
    assert not MCPRelease(str(config_path), env_file=None).entries


@pytest.mark.parametrize("change", ["binding", "write", "collision", "remote_ref"])
def test_invalid_configuration_fails_before_runtime(config_path, change):
    """拼写错误、未支持写入、名字冲突和外部 Schema 引用不进入工具目录。"""
    text = config_path.read_text()
    if change == "binding":
        text = text.replace('"hotel.search", "hotel.detail"', '"hotel.missing"')
    elif change == "write":
        text = text.replace('side_effect = "read"', 'side_effect = "write"')
    elif change == "collision":
        text = text.replace(
            'contracts = "hotel.json"',
            'contracts = "hotel.json"\naliases = {search="same", detail="same"}',
        )
    else:
        path = config_path.parent / "hotel.json"
        raw = json.loads(path.read_text())
        raw["tools"][0]["inputSchema"]["properties"]["query"] = {
            "$ref": "https://untrusted.example/schema"
        }
        path.write_text(json.dumps(raw))
    config_path.write_text(text)
    with pytest.raises(ValueError):
        MCPRelease(str(config_path), env_file=None)


def test_defaults_overrides_and_explicit_no_dotenv(config_path, monkeypatch):
    """覆盖只影响指定服务；URL 环境引用不绕过调用方的 dotenv 设置。"""
    text = config_path.read_text().replace(
        'url = "https://hotel.example/mcp"', 'url_env = "TEST_MCP_URL"\ntimeout_seconds = 9'
    )
    config_path.write_text(text)
    (config_path.parent / ".env").write_text("TEST_MCP_URL=https://hotel.example/mcp\n")
    monkeypatch.delenv("TEST_MCP_URL", raising=False)
    with pytest.raises(ValueError):
        MCPRelease(str(config_path), env_file=None)
    release = MCPRelease(str(config_path), env_file=str(config_path.parent / ".env"))
    assert release.entries["hotel.search"].limits.timeout_seconds == 9
    assert release.entries["other.search"].limits.timeout_seconds == 5
    assert MCPConfiguration.from_file(str(config_path)).defaults.timeout_seconds == 5
