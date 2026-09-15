"""合成目录与 HTTP 协议替身；真实 SDK 负责初始化、分页和工具调用。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.mcp import MCPManifest
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.mcp.configuration import manifest_json

SEARCH = {
    "name": "search",
    "description": "Search synthetic hotels for an explicit destination.",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"hotel_id": {"type": "string"}},
        "required": ["hotel_id"],
    },
}
DETAIL = {
    "name": "detail",
    "description": "Read synthetic hotel details from its identifier.",
    "inputSchema": {
        "type": "object",
        "properties": {"hotel_id": {"type": "string"}},
        "required": ["hotel_id"],
    },
}


@pytest.fixture
def config_path(tmp_path):
    """临时配置包含两个同名远端工具，仅一个服务绑定根 Agent。"""
    path = tmp_path / "mcp.toml"
    sections = ["[defaults]\ntimeout_seconds = 5\n"]
    for alias in ("hotel", "other"):
        sections.append(f'''
[servers.{alias}]
enabled = true
url = "https://{alias}.example/mcp"
allowed_hosts = ["{alias}.example"]
allowed_tools = ["search", "detail"]
contracts = "{alias}.json"
[servers.{alias}.auth]
type = "bearer"
token_env = "TEST_MCP_KEY"
[servers.{alias}.policy]
side_effect = "read"
required_scopes = ["travel:read"]
egress = "external"
allowed_data_classes = ["public", "internal"]
''')
        manifest = MCPManifest(
            server=alias,
            endpoint=f"https://{alias}.example/mcp",
            protocol_version="2025-11-25",
            server_info={"name": "synthetic-mcp", "version": "1.0.0"},
            tools=[SEARCH, DETAIL],
        )
        (tmp_path / f"{alias}.json").write_text(manifest_json(manifest))
    sections.append('[agents.finance_agent]\nmcp_tools = ["hotel.search", "hotel.detail"]\n')
    path.write_text("\n".join(sections))
    return path


@pytest.fixture
def settings(config_path, tmp_path):
    """不读取用户环境，使用临时数据库和已导入的合成 MCP 定义。"""
    return FinanceClawSettings(
        _env_file=None,
        environment="test",
        offline_model=True,
        debug_full_io=False,
        mcp_config_path=str(config_path),
        database_url=f"sqlite:///{tmp_path}/app.db",
        database_auto_create_schema=True,
        artifact_root=str(tmp_path / "artifacts"),
    )


@pytest.fixture
def context():
    """业务运行身份；没有把认证头或历史会话传到 MCP 工具参数中。"""
    return ExecutionContext(
        tenant_id="mcp-test",
        subject_id="mcp-user",
        turn_id="mcp-turn",
        scopes={"travel:read", "tools:read", "artifacts:read"},
        request_clock="2026-09-15T08:00:00+08:00",
    )


@pytest.fixture
def protocol(monkeypatch):
    """仅替换 HTTP 传输；客户端和 JSON-RPC 编解码使用正式 MCP SDK。"""
    state = SimpleNamespace(
        requests=[],
        tools=deepcopy([SEARCH, DETAIL]),
        status=None,
        is_error=False,
        structured_only=False,
        result_override=None,
        page_failure=False,
        wait_for_call=False,
        call_entered=asyncio.Event(),
        release_call=asyncio.Event(),
        closed=0,
    )

    async def handle(request):
        """模拟分页目录和两个依赖查询，允许测试注入协议错误。"""
        body = json.loads(request.content) if request.content else {}
        state.requests.append((request, body))
        if state.status:
            return httpx.Response(state.status, text="private upstream error", request=request)
        if "id" not in body:
            return httpx.Response(202, request=request)
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "serverInfo": {"name": "synthetic-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            second = body.get("params", {}).get("cursor") == "next"
            if second and state.page_failure:
                return httpx.Response(503, request=request)
            result = {"tools": state.tools[1:] if second else state.tools[:1]}
            if not second:
                result["nextCursor"] = "next"
        elif method == "tools/call":
            if state.wait_for_call:
                state.call_entered.set()
                await state.release_call.wait()
            name = body["params"]["name"]
            result = state.result_override or {
                "content": []
                if state.structured_only
                else [{"type": "text", "text": "synthetic hotel"}],
                "structuredContent": {"hotel_id": "hotel-1", "name": name},
                "isError": state.is_error,
            }
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    original = httpx.AsyncClient

    class MockClient(original):
        """记录 session 清理，不执行任何外部网络操作。"""

        def __init__(self, *args, **kwargs):
            """使用真实 HTTP 客户端的协议行为，仅注入本地 MockTransport。"""
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(*args, **kwargs)

        async def __aexit__(self, *args):
            """记录上下文退出时的连接释放。"""
            state.closed += 1
            await super().__aexit__(*args)

    monkeypatch.setattr(
        "financeclaw.agent_server.tools.mcp_transport.httpx.AsyncClient", MockClient
    )
    monkeypatch.setenv("TEST_MCP_KEY", "synthetic-mcp-credential")
    return state
