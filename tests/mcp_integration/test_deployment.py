"""验证部署前固定 MCP 定义、环境一致性和失败时停止后续部署。"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from financeclaw.agent_server.tools.policy import TransientToolError
from financeclaw.shared.mcp.configuration import MCPRelease
from scripts import deploy, mcp_catalog


@pytest.mark.asyncio
async def test_prepare_reuses_existing_manifests_offline(config_path, monkeypatch):
    """已有定义不需要有效凭据，也不会因远端更新而暗中替换文件。"""
    monkeypatch.delenv("TEST_MCP_KEY", raising=False)
    before = {path: path.read_bytes() for path in config_path.parent.glob("*.json")}

    async def unexpected_discovery(*args, **kwargs):
        """离线复用发生联网即失败。"""
        pytest.fail("existing manifests should be checked offline")

    monkeypatch.setattr(mcp_catalog, "discover", unexpected_discovery)
    await mcp_catalog.prepare(config_path)
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.asyncio
async def test_prepare_imports_missing_allowed_tools_only(config_path, protocol):
    """首次部署自动补齐所选工具；额外远端工具和业务查询不参与准备。"""
    path = config_path.parent / "hotel.json"
    path.unlink()
    protocol.tools.append({"name": "unselected", "inputSchema": {"type": "object"}})
    await mcp_catalog.prepare(config_path)
    assert {tool["name"] for tool in json.loads(path.read_text())["tools"]} == {"search", "detail"}
    assert len(MCPRelease(str(config_path)).entries) == 4
    assert {request.url.host for request, _ in protocol.requests} == {"hotel.example"}
    assert not any(body.get("method") == "tools/call" for _, body in protocol.requests)


@pytest.mark.asyncio
async def test_prepare_refresh_skips_disabled_services(config_path, protocol):
    """显式刷新更新启用服务，关闭服务即使没有文件也不连接。"""
    config_path.write_text(
        config_path.read_text().replace(
            "[servers.other]\nenabled = true", "[servers.other]\nenabled = false"
        )
    )
    (config_path.parent / "other.json").unlink()
    protocol.tools[0]["description"] = "Updated remote description"
    await mcp_catalog.prepare(config_path, refresh=True)
    entry = MCPRelease(str(config_path)).entries["hotel.search"]
    assert entry.definition.description == "Updated remote description"
    assert {request.url.host for request, _ in protocol.requests} == {"hotel.example"}
    assert not (config_path.parent / "other.json").exists()


@pytest.mark.asyncio
async def test_prepare_remote_failure_preserves_entire_batch(config_path, protocol, monkeypatch):
    """第二个服务失败时，第一个已成功获取的新定义也不会覆盖旧文件。"""
    before = {path: path.read_bytes() for path in config_path.parent.glob("*.json")}
    protocol.tools[0]["description"] = "Updated remote description"
    discover = mcp_catalog.discover

    async def fail_second(configuration, alias, **kwargs):
        """保留真实 SDK 调用，在第二个服务的分页中模拟中断。"""
        protocol.page_failure = alias == "other"
        return await discover(configuration, alias, **kwargs)

    monkeypatch.setattr(mcp_catalog, "discover", fail_second)
    with pytest.raises(TransientToolError):
        await mcp_catalog.prepare(config_path, refresh=True)
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.asyncio
async def test_prepare_checks_batch_before_writing(config_path, protocol):
    """跨服务命名冲突在写文件前被既有目录检查发现。"""
    before = {path: path.read_bytes() for path in config_path.parent.glob("*.json")}
    config_path.write_text(
        config_path.read_text().replace(
            'allowed_tools = ["search", "detail"]',
            'allowed_tools = ["search", "detail"]\naliases = {search = "same_name"}',
        )
    )
    with pytest.raises(ValueError, match="duplicate MCP model tool name"):
        await mcp_catalog.prepare(config_path, refresh=True)
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.asyncio
async def test_prepare_does_not_silently_replace_invalid_existing_file(config_path, protocol):
    """已有文件损坏时要求显式处理，不把重新联网作为隐式修复。"""
    path = config_path.parent / "hotel.json"
    path.write_text("invalid manifest")
    with pytest.raises(ValidationError):
        await mcp_catalog.prepare(config_path)
    assert path.read_text() == "invalid manifest" and not protocol.requests


@pytest.fixture
def deployment(config_path, tmp_path):
    """构造 Dockerfile 支持的目录和 Compose 解析后的容器环境。"""
    root = tmp_path / "repository"
    contracts = root / "config" / "mcp"
    contracts.mkdir(parents=True)
    text = config_path.read_text()
    for alias in ("hotel", "other"):
        text = text.replace(f'contracts = "{alias}.json"', f'contracts = "mcp/{alias}.json"')
        shutil.copy(config_path.parent / f"{alias}.json", contracts / f"{alias}.json")
    (root / "config" / "mcp.toml").write_text(text)
    services = {
        "api": {"build": {"context": str(root)}, "environment": {}},
        "worker": {
            "build": {"context": str(root)},
            "environment": {"TEST_MCP_KEY": "synthetic-worker-credential"},
        },
        "postgres": {"image": "postgres:16", "environment": {}},
    }
    args = SimpleNamespace(
        env_file="custom env", files=None, refresh_mcp=False, prepare_only=False, build_proxy=None
    )
    return SimpleNamespace(root=root, services=services, args=args, calls=[])


def compose_stub(deployment, monkeypatch, *, failure=None):
    """模拟 Docker 命令，MCP 准备仍执行真实解析、SDK 和写盘逻辑。"""

    def run(command, **kwargs):
        """记录命令和环境，允许验证任一步失败后不继续部署。"""
        stage = (
            "prepare"
            if command[0] != "docker"
            else next(name for name in ("config", "build", "up", "ps") if name in command)
        )
        deployment.calls.append((stage, command, kwargs))
        if stage == "build" and command[-2].endswith("compose.proxy.json"):
            deployment.proxy_overlay = json.loads(Path(command[-2]).read_text())
        if stage == failure:
            raise subprocess.CalledProcessError(7, command)
        if stage == "config":
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"services": deployment.services})
            )
        if stage == "prepare":
            path = command[command.index("--config") + 1]
            assert command[command.index("--env-file") + 1] == ""
            with patch.dict(os.environ, kwargs["env"], clear=True):
                asyncio.run(mcp_catalog.prepare(path, refresh="--refresh" in command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deploy.subprocess, "run", run)


def test_deploy_uses_compose_environment_and_orders_steps(deployment, protocol, monkeypatch):
    """一条命令按实际容器凭据导入，再构建、启动并显示状态，保留组合文件。"""
    (deployment.root / "config" / "mcp" / "hotel.json").unlink()
    deployment.args.files = ["compose.yml", "compose.taibu.yml"]
    compose_stub(deployment, monkeypatch)
    deploy.deploy(deployment.args, root=deployment.root)
    assert [stage for stage, _, _ in deployment.calls] == ["config", "prepare", "build", "up", "ps"]
    for stage, command, kwargs in deployment.calls:
        assert kwargs["cwd"] == deployment.root
        assert "synthetic-worker-credential" not in " ".join(command)
        if stage != "prepare":
            env_file = str(deployment.root / "custom env")
            assert command[command.index("--env-file") + 1] == env_file
            assert kwargs["env"]["FINANCECLAW_ENV_FILE"] == env_file
            assert str(deployment.root / "compose.taibu.yml") in command
    assert all(
        request.headers["authorization"] == "Bearer synthetic-worker-credential"
        for request, _ in protocol.requests
    )
    assert deployment.calls[-2][1][-3:] == ["up", "-d", "--no-build"]


@pytest.mark.parametrize("failure", ["prepare", "build", "up"])
def test_deploy_failure_stops_later_steps(deployment, monkeypatch, failure):
    """准备或构建失败不会启动容器，启动失败不会假报成功。"""
    compose_stub(deployment, monkeypatch, failure=failure)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy(deployment.args, root=deployment.root)
    assert deployment.calls[-1][0] == failure


def test_deploy_prepare_only_refreshes_without_building(deployment, protocol, monkeypatch):
    """显式更新定义可先落盘供审阅，准备模式不构建或启动。"""
    deployment.args.prepare_only = True
    deployment.args.refresh_mcp = True
    protocol.tools[0]["description"] = "Updated remote description"
    compose_stub(deployment, monkeypatch)
    deploy.deploy(deployment.args, root=deployment.root)
    assert [stage for stage, _, _ in deployment.calls] == ["config", "prepare"]
    release = MCPRelease(str(deployment.root / "config" / "mcp.toml"))
    assert release.entries["hotel.search"].definition.description == "Updated remote description"


def test_preparation_does_not_inherit_host_only_credentials(deployment, monkeypatch):
    """本机配置的 Key 没有传入容器时，准备环境不能借用它掩盖配置遗漏。"""
    monkeypatch.setenv("TEST_MCP_KEY", "synthetic-host-only-credential")
    deployment.services["worker"]["environment"].clear()
    _, environment = deploy.preparation_inputs(deployment.services, deployment.root)
    assert "TEST_MCP_KEY" not in environment


def test_build_proxy_is_passed_only_to_build_without_value_in_command(deployment, monkeypatch):
    """构建代理不写入命令参数值，不改变 MCP 准备或容器启动的代理环境。"""
    deployment.args.build_proxy = "http://build-proxy.example:7890"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:7890")
    compose_stub(deployment, monkeypatch)
    deploy.deploy(deployment.args, root=deployment.root)
    for stage, command, kwargs in deployment.calls:
        assert deployment.args.build_proxy not in " ".join(command)
        assert kwargs["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
        assert kwargs["env"]["https_proxy"] == "http://127.0.0.1:7890"
        if stage == "build":
            assert kwargs["env"]["FINANCECLAW_BUILD_PROXY"] == deployment.args.build_proxy
            assert not Path(command[-2]).exists()
        else:
            assert not any(item.endswith("compose.proxy.json") for item in command)
    assert set(deployment.proxy_overlay["services"]) == {"api", "worker"}
    assert deployment.proxy_overlay["services"]["api"]["build"]["args"]["HTTP_PROXY"] == (
        "${FINANCECLAW_BUILD_PROXY}"
    )


@pytest.mark.parametrize("kind", ["different_config", "outside_image", "endpoint", "contracts"])
def test_deploy_rejects_inconsistent_packaging(deployment, monkeypatch, kind):
    """避免检查的是一份定义，容器最终使用另一份或根本未打包的定义。"""
    api = deployment.services["api"]["environment"]
    path = deployment.root / "config" / "mcp.toml"
    if kind == "different_config":
        api["FINANCECLAW_MCP_CONFIG_PATH"] = "config/other.toml"
    elif kind == "outside_image":
        api["FINANCECLAW_MCP_CONFIG_PATH"] = "/tmp/mcp.toml"
    elif kind == "endpoint":
        path.write_text(
            path.read_text().replace('url = "https://hotel.example/mcp"', 'url_env = "MCP_URL"')
        )
        api["MCP_URL"] = "https://hotel.example/mcp"
    else:
        path.write_text(
            path.read_text().replace('contracts = "mcp/hotel.json"', 'contracts = "hotel.json"')
        )
    compose_stub(deployment, monkeypatch)
    with pytest.raises(deploy.DeploymentError):
        deploy.deploy(deployment.args, root=deployment.root)
    assert [stage for stage, _, _ in deployment.calls] == ["config"]
