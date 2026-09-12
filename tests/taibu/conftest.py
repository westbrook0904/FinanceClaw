"""独立仓储和固定合成样例；测试不请求公共服务或用户资料。"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.turn_support import seed_execution

BAZI = {
    "gender": "male",
    "calendar_type": "solar",
    "birth_year": 1990,
    "birth_month": 1,
    "birth_day": 15,
    "birth_hour": 9,
    "birth_minute": 0,
    "time_basis": "china_standard",
    "solar_time": "standard",
}


@pytest.fixture
def settings(tmp_path):
    """无用户环境文件、可持久化且关闭出生资料追踪的配置。"""
    return FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        taibu_enabled=True,
        debug_full_io=False,
        langsmith_hide_inputs=True,
        langsmith_hide_outputs=True,
        database_url=f"sqlite+pysqlite:///{tmp_path}/taibu.db",
        artifact_root=str(tmp_path / "artifacts"),
        database_auto_create_schema=True,
    )


@pytest.fixture
def samples():
    """独立保存的公开合成样例，包含原始文本、JSON 与地点警告。"""
    return json.loads(Path(__file__).with_name("samples.json").read_text())


class StubRemote:
    """只替换外部计算；保留真实工具、治理、归档和图执行。"""

    def __init__(self, results):
        """保存各测试独立的返回和调用记录。"""
        self.results = deepcopy(results)
        self.calls = []
        self.error = None

    async def call(self, name, arguments):
        """记录精确参数，并返回一个实际的 MCP 结果模型。"""
        self.calls.append((name, deepcopy(arguments)))
        if self.error:
            raise self.error
        return CallToolResult.model_validate(self.results[name])


@pytest.fixture
def stack(settings, samples):
    """创建有效 Turn 归属，确保 Artifact 确实落库并可进行跨租户验证。"""
    resources = build_resources(settings, enable_persistence=True)
    context = seed_execution(
        resources.conversation_repository.execution,
        ExecutionContext(
            tenant_id="tenant-taibu",
            subject_id="user-taibu",
            turn_id="taibu-turn",
            scopes={"taibu:read", "artifacts:read"},
            data_classification="confidential",
            request_clock="2026-09-11T23:59:00+08:00",
        ),
        {"limits": {"model": 100, "tool": 100, "command": 100}},
    )
    yield SimpleNamespace(
        settings=settings,
        resources=resources,
        context=context,
        artifacts=resources.artifact_service,
        remote=StubRemote(samples),
    )
    resources.database.close()
