"""太卜有界 HTTP MCP 适配：固定契约、隔离 session、保留原始双通道结果。"""

import asyncio
import json
import time
from threading import Lock

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import types

from financeclaw.agent_server.tools.policy import TransientToolError
from financeclaw.kernel.taibu import TaibuError
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.taibu import TAIBU_SERVER_VERSION, contract_hash, remote_contracts


def _leaves(error: Exception) -> list[Exception]:
    """展开 TaskGroup 异常，同时让 BaseException 类型的取消原样向外传播。"""
    if isinstance(error, ExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _leaves(child)]
    return [error]


def _mapped_error(error: Exception) -> Exception:
    """仅把可重试网络错误映射为瞬态异常，不暴露协议正文和调用参数。"""
    failures = _leaves(error)
    for item in failures:
        if isinstance(item, TaibuError):
            return item
        if isinstance(item, httpx.HTTPStatusError):
            status = item.response.status_code
            if status == 429:
                retry_after = item.response.headers.get("retry-after", "")
                delay = (
                    f"；建议等待 {retry_after} 秒"
                    if retry_after.isdigit() and len(retry_after) < 6
                    else ""
                )
                return TaibuError("TAIBU_RATE_LIMITED", f"太卜服务限流，本次不自动重试{delay}。")
            if status < 500:
                return TaibuError("TAIBU_HTTP_REJECTED", f"太卜连接被拒绝（HTTP {status}）。")
    if all(
        isinstance(item, TimeoutError | OSError | httpx.TransportError)
        or (isinstance(item, httpx.HTTPStatusError) and item.response.status_code >= 500)
        for item in failures
    ):
        return TransientToolError("TAIBU_UNAVAILABLE: 太卜服务暂时不可用。")
    return TaibuError("TAIBU_PROTOCOL_ERROR", "太卜未返回符合已发布契约的协议响应。")


class TaibuMCPClient:
    """只共享无状态配置和契约校验缓存；每次尝试建立独立的 SDK session。"""

    def __init__(self, settings: FinanceClawSettings, *, client=None):
        """允许测试注入协议客户端；生产连接只能来自已校验的部署配置。"""
        settings.validate_taibu()
        self.timeout_seconds = settings.taibu_timeout_seconds
        self.result_max_bytes = settings.taibu_result_max_bytes
        self.cache_seconds = settings.taibu_contract_cache_seconds
        self.allowed_tools = settings.taibu_allowed_tools
        self.contracts = remote_contracts()
        self._verified_until = 0.0
        self._cache_lock = Lock()
        external = settings.taibu_egress == "external"

        def http_client_factory(headers=None, timeout=None, auth=None):
            """内部流量不继承系统代理；禁止重定向绕过部署允许的端点。"""
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout or self.timeout_seconds,
                auth=auth,
                follow_redirects=False,
                trust_env=external,
            )

        self._client = client or MultiServerMCPClient(
            {
                "taibu": {
                    "transport": "streamable_http",
                    "url": settings.taibu_mcp_url,
                    "timeout": self.timeout_seconds,
                    "sse_read_timeout": self.timeout_seconds,
                    "httpx_client_factory": http_client_factory,
                }
            }
        )

    async def _verify(self, session) -> None:
        """检查有界工具目录，缓存成功摘要；重复、缺失或漂移都禁止执行。"""
        with self._cache_lock:
            if time.monotonic() < self._verified_until:
                return
        found = {}
        cursor = None
        for _ in range(4):
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in found:
                    raise TaibuError("TAIBU_CONTRACT_MISMATCH", "远端存在重复工具名称。")
                found[tool.name] = tool.model_dump(mode="json")
            cursor = page.nextCursor
            if cursor is None:
                break
        else:
            raise TaibuError("TAIBU_CONTRACT_MISMATCH", "远端工具目录超过已支持的分页范围。")
        for name in self.allowed_tools:
            if name not in found or contract_hash(found[name]) != contract_hash(
                self.contracts[name]
            ):
                raise TaibuError("TAIBU_CONTRACT_MISMATCH", "太卜工具契约变化，需要重新验收发布。")
        with self._cache_lock:
            self._verified_until = time.monotonic() + self.cache_seconds

    async def call(self, name: str, arguments: dict) -> types.CallToolResult:
        """在同一尝试预算内初始化、验证、调用并清理，不自行重试。"""
        if name not in self.allowed_tools:
            raise TaibuError("TAIBU_TOOL_DENIED", "该太卜工具未在本次发布中开放。")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._client.session("taibu", auto_initialize=False) as session:
                    initialized = await session.initialize()
                    if (
                        initialized.serverInfo.name != "taibu-mcp-online"
                        or initialized.serverInfo.version != TAIBU_SERVER_VERSION
                    ):
                        raise TaibuError("TAIBU_VERSION_MISMATCH", "太卜服务版本与固定发布不一致。")
                    await self._verify(session)
                    # SDK call_tool 会按即时发现的 Schema 先校验并可能丢失诊断原文。
                    # 使用 SDK 公开请求 API，随后由本地固定契约验证原始响应。
                    result = await session.send_request(
                        types.ClientRequest(
                            types.CallToolRequest(
                                params=types.CallToolRequestParams(name=name, arguments=arguments)
                            )
                        ),
                        types.CallToolResult,
                    )
                    size = len(
                        json.dumps(result.model_dump(mode="json"), ensure_ascii=False).encode()
                    )
                    if size > self.result_max_bytes:
                        raise TaibuError("TAIBU_RESULT_TOO_LARGE", "太卜原始结果超过本次返回预算。")
                    return result
        except Exception as error:
            raise _mapped_error(error) from None
