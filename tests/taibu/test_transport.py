"""官方 SDK 经模拟 HTTP 传输验证协商、契约、错误和取消。"""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from financeclaw.agent_server.tools.mcp_client import TaibuMCPClient
from financeclaw.agent_server.tools.policy import TransientToolError
from financeclaw.kernel.taibu import TaibuError
from financeclaw.shared.releases.taibu import remote_contracts


class MCPHTTP:
    """只模拟 HTTP 服务，协议编解码和异步生命周期全部运行实际 SDK。"""

    def __init__(self, samples):
        """提供可逐项制造失效的固定响应。"""
        self.samples = deepcopy(samples)
        self.tools = list(remote_contracts().values())
        self.version = "3.1.1"
        self.calls = []
        self.status = 200
        self.delay = 0
        self.entered = asyncio.Event()

    async def handle(self, request):
        """实现初始化、通知、工具清单和调用的无状态 POST 协议。"""
        if request.method != "POST":
            return httpx.Response(405)
        value = json.loads(request.content)
        method = value["method"]
        self.calls.append(method)
        if "id" not in value:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "taibu-mcp-online", "version": self.version},
            }
        elif method == "tools/list":
            result = {"tools": self.tools}
        else:
            self.entered.set()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.status != 200:
                return httpx.Response(
                    self.status, headers={"retry-after": "60"}, text="private-remote-body"
                )
            result = self.samples[value["params"]["name"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": value["id"], "result": result})

    def client(self, settings):
        """将内存 HTTP transport 注入真实 MultiServerMCPClient。"""

        def factory(headers=None, timeout=None, auth=None):
            """保持 SDK 需要的客户端签名和关闭语义。"""
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handle),
                headers=headers,
                timeout=timeout,
                auth=auth,
            )

        return TaibuMCPClient(
            settings,
            client=MultiServerMCPClient(
                {
                    "taibu": {
                        "transport": "streamable_http",
                        "url": settings.taibu_mcp_url,
                        "httpx_client_factory": factory,
                    }
                }
            ),
        )


@pytest.mark.asyncio
async def test_real_sdk_preserves_json_and_caches_contract_check(settings, samples):
    """两次独立 session 都握手，但缓存期内只发现一次工具。"""
    http = MCPHTTP(samples)
    client = http.client(settings)
    for _ in range(2):
        value = await client.call("almanac", {"date": "2026-09-12"})
        assert value.structuredContent == samples["almanac"]["structuredContent"]
    assert http.calls.count("initialize") == 2
    assert http.calls.count("tools/list") == 1
    assert http.calls.count("tools/call") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,transient",
    [
        (429, "TAIBU_RATE_LIMITED", False),
        (503, "TAIBU_UNAVAILABLE", True),
        (403, "TAIBU_HTTP_REJECTED", False),
        (302, "TAIBU_HTTP_REJECTED", False),
    ],
)
async def test_http_errors_are_classified_without_exposing_response(
    settings, samples, status, code, transient
):
    """限流和拒绝不立即重试，5xx 可由外层重试，远端正文不进入错误。"""
    http = MCPHTTP(samples)
    http.status = status
    with pytest.raises(TransientToolError if transient else TaibuError, match=code) as caught:
        await http.client(settings).call("almanac", {"date": "2026-09-12"})
    assert "private-remote-body" not in str(caught.value)
    assert http.calls.count("tools/call") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["version", "schema", "missing", "duplicate"])
async def test_changed_release_cannot_execute_tool(settings, samples, kind):
    """服务端变化在 tools/call 前就被拒绝。"""
    http = MCPHTTP(samples)
    if kind == "version":
        http.version = "99.0.0"
    elif kind == "schema":
        http.tools[0]["inputSchema"]["properties"]["new"] = {"type": "string"}
    elif kind == "missing":
        http.tools.pop()
    else:
        http.tools.append(http.tools[0])
    with pytest.raises(TaibuError, match="TAIBU_(VERSION|CONTRACT)_MISMATCH"):
        await http.client(settings).call("almanac", {"date": "2026-09-12"})
    assert "tools/call" not in http.calls


@pytest.mark.asyncio
async def test_timeout_covers_session_and_does_not_retry_itself(settings, samples):
    """尝试级超时涵盖实际协议调用并正常清理任务。"""
    settings.taibu_timeout_seconds = 0.05
    http = MCPHTTP(samples)
    http.delay = 5
    with pytest.raises(TransientToolError, match="TAIBU_UNAVAILABLE"):
        await http.client(settings).call("almanac", {"date": "2026-09-12"})
    assert http.calls.count("tools/call") == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_an_availability_failure(settings, samples):
    """调用者取消保留 CancelledError，禁止转换为可自动重试异常。"""
    http = MCPHTTP(samples)
    http.delay = 5
    task = asyncio.create_task(http.client(settings).call("almanac", {"date": "2026-09-12"}))
    await asyncio.wait_for(http.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_raw_result_limit_is_checked_before_returning(settings, samples):
    """响应大小与模型投影分别限额。"""
    settings.taibu_result_max_bytes = 8192
    http = MCPHTTP(samples)
    http.samples["almanac"]["content"][0]["text"] = "大结果" * 5000
    with pytest.raises(TaibuError, match="TAIBU_RESULT_TOO_LARGE"):
        await http.client(settings).call("almanac", {"date": "2026-09-12"})


@pytest.mark.asyncio
async def test_contract_cache_expiration_checks_drift(settings, samples):
    """有界缓存到期后复验，发现变化即阻止后续实际调用。"""
    http = MCPHTTP(samples)
    client = http.client(settings)
    await client.call("almanac", {"date": "2026-09-12"})
    client._verified_until = 0
    http.tools[0]["annotations"]["readOnlyHint"] = False
    with pytest.raises(TaibuError, match="TAIBU_CONTRACT_MISMATCH"):
        await client.call("almanac", {"date": "2026-09-12"})
    assert http.calls.count("tools/list") == 2
    assert http.calls.count("tools/call") == 1


@pytest.mark.asyncio
async def test_connection_failure_uses_transient_error(settings, samples):
    """真实 SDK 传输中的连接中断交给统一重试，错误不泄露原始细节。"""
    http = MCPHTTP(samples)

    async def broken(request):
        """模拟连接层失败，保留 httpx 的实际异常类型。"""
        raise httpx.ConnectError("private-connection-detail", request=request)

    http.handle = broken
    with pytest.raises(TransientToolError, match="TAIBU_UNAVAILABLE") as caught:
        await http.client(settings).call("almanac", {"date": "2026-09-12"})
    assert "private-connection-detail" not in str(caught.value)
