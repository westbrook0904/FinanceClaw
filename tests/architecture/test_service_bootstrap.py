"""通过独立进程与真实装配防止跨服务隐式初始化和发布漂移。"""

import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    "entry,forbidden",
    [
        (
            "financeclaw",
            ["financeclaw.bff", "financeclaw.coordination", "financeclaw.agent_server"],
        ),
        ("financeclaw.bff.bootstrap", ["financeclaw.agent_server", "langgraph.graph"]),
        (
            "financeclaw.coordination.bootstrap",
            ["financeclaw.bff", "financeclaw.agent_server", "langgraph.graph"],
        ),
        ("financeclaw.agent_server.bootstrap", ["financeclaw.bff", "financeclaw.coordination"]),
    ],
)
def test_imports_do_not_initialize_other_services(entry: str, forbidden: list[str]) -> None:
    """冷导入不得加载其他服务的实现、图注册入口或飞书 WebSocket SDK。"""
    script = f"""
import importlib
import sys
import threading
before = set(threading.enumerate())
importlib.import_module({entry!r})
for prefix in {forbidden!r} + ["lark_channel", "financeclaw.agent_server.graphs.server_graphs"]:
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules), prefix
assert set(threading.enumerate()) == before
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("ziwei_enabled", [False, True])
def test_bff_can_boot_without_agent_implementation(tmp_path: Path, ziwei_enabled: bool) -> None:
    """即使启用领域 Agent，BFF 装配也不能初始化图、工具、模型或排盘依赖。"""
    script = f"""
import sys
from financeclaw.bff.bootstrap import create_default_app
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
settings = FinanceClawSettings(
    _env_file=None, environment="test", offline_model=False, debug_full_io=False,
    langsmith_hide_inputs=True, langsmith_hide_outputs=True,
    ziwei_enabled={ziwei_enabled!r}, ziwei_convention="x-iztro-civil-candidate@1.0.0",
    ziwei_hmac_key="synthetic-package-boundary-test-key-0123456789",
    database_url={f"sqlite:///{tmp_path}/bff.db"!r}, artifact_root={str(tmp_path / "artifacts")!r},
    database_auto_create_schema=True, feishu_enabled=False,
)
app = create_default_app(settings)
try:
    assert app.state.financeclaw_database.ping()
    assert any(route.path == "/v1/conversations" for route in app.routes)
    for prefix in ["financeclaw.agent_server", "langgraph.graph", "x_iztro", "lark_channel"]:
        loaded = [name for name in sys.modules if name == prefix or name.startswith(prefix + ".")]
        assert not loaded, loaded
finally:
    app.state.financeclaw_database.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("ziwei_enabled", [False, True])
def test_coordination_and_runtime_share_exact_release_contracts(
    tmp_path: Path, ziwei_enabled: bool
) -> None:
    """独立装配的发布快照必须一致，工作流图仅存在于执行端。"""
    if ziwei_enabled:
        pytest.importorskip("x_iztro")
        pytest.importorskip("tzdata")
    from financeclaw.agent_server.bootstrap import build_components
    from financeclaw.coordination.bootstrap import build_coordination
    from financeclaw.shared.infrastructure.resources import build_resources
    from financeclaw.shared.infrastructure.settings import FinanceClawSettings

    settings = FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        debug_full_io=False,
        langsmith_hide_inputs=True,
        langsmith_hide_outputs=True,
        ziwei_enabled=ziwei_enabled,
        ziwei_convention="x-iztro-civil-candidate@1.0.0",
        ziwei_hmac_key="synthetic-package-boundary-test-key-0123456789",
        database_url=f"sqlite:///{tmp_path}/shared.db",
        artifact_root=str(tmp_path / "artifacts"),
        database_auto_create_schema=True,
    )
    resources = build_resources(settings, enable_persistence=True)
    try:
        coordination = build_coordination(resources=resources)
        runtime = build_components(resources=resources)
        assert runtime.conversation_repository is coordination.conversations.repository
        assert coordination.conversations.execution.sessions is resources.database.session_factory
        assert coordination.delegations.repository._sessions is resources.database.session_factory
        assert coordination.workflows.repository._sessions is resources.database.session_factory
        assert set(runtime.agent_profiles) == set(coordination.releases.agent_profiles)
        for key, declared in coordination.releases.agent_profiles.items():
            actual = runtime.agent_profiles[key]
            assert actual.model_dump(mode="json") == declared.model_dump(mode="json")
            for attr in ("input_schema", "output_schema"):
                schema = getattr(actual, attr)
                assert (schema.model_json_schema() if schema else None) == (
                    getattr(declared, attr).model_json_schema() if getattr(declared, attr) else None
                )
        assert set(runtime.tool_catalog) == set(coordination.releases.tool_catalog)
        for key, declared in coordination.releases.tool_catalog.items():
            assert runtime.tool_catalog[key].governance == declared.governance
            assert not hasattr(declared, "tool")
        for key, declared in coordination.releases.workflow_catalog.items():
            actual = runtime.workflow_catalog[key]
            assert actual.graph is not None and not hasattr(declared, "graph")
            assert {field.name: getattr(declared, field.name) for field in fields(declared)} == {
                field.name: getattr(actual, field.name) for field in fields(declared)
            }
    finally:
        resources.database.close()
