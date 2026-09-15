"""通用 HTTP MCP 传输：按调用建立 session，保留 SDK 原始结果，不自行重试。"""

import asyncio

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import types

from financeclaw.agent_server.tools.mcp_errors import MCPError, MCPUnavailableError
from financeclaw.kernel.mcp import MCPDefaults, MCPManifest, MCPServer, MCPToolDefinition
from financeclaw.shared.mcp.configuration import (
    MCPEntry,
    endpoint,
    environment_value,
    validate_definition,
)
from financeclaw.shared.releases.fingerprint import configuration_fingerprint


def _leaves(error):
    """展开 SDK TaskGroup 的异常，取消仍由 BaseException 原样传播。"""
    if isinstance(error, ExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _leaves(child)]
    return [error]


def mapped_error(error: Exception) -> Exception:
    """只将网络或服务端瞬态错误交给现有只读重试层。"""
    failures = _leaves(error)
    for item in failures:
        if isinstance(item, MCPError):
            return item
        if isinstance(item, httpx.HTTPStatusError) and item.response.status_code < 500:
            status = item.response.status_code
            code = "MCP_RATE_LIMITED" if status == 429 else "MCP_HTTP_REJECTED"
            return MCPError(f"{code}: MCP 连接被拒绝（HTTP {status}），请核对服务权限。")
    if all(
        isinstance(item, TimeoutError | OSError | httpx.TransportError)
        or (isinstance(item, httpx.HTTPStatusError) and item.response.status_code >= 500)
        for item in failures
    ):
        return MCPUnavailableError("MCP_UNAVAILABLE: MCP 服务暂时不可用。")
    return MCPError("MCP_PROTOCOL_ERROR: MCP 连接或响应不符合已发布协议。")


class MCPTransport:
    """只保存非密钥连接；真实凭据在调用时从执行环境解析。"""

    def __init__(self, alias: str, server: MCPServer, limits: MCPDefaults, *, env_file=None):
        """共用于维护命令和业务执行，构造时不联网或解析认证值。"""
        self.alias = alias
        self.server = server
        self.limits = limits
        self.env_file = env_file

    def _client(self, url: str):
        """用 SDK 创建标准 HTTP MCP 客户端，禁止跨端点重定向认证。"""
        auth = self.server.auth
        try:
            headers = {
                name: environment_value(ref, env_file=self.env_file, secret=True)
                for name, ref in auth.headers_env.items()
            }
            if auth.type == "bearer":
                headers["Authorization"] = "Bearer " + environment_value(
                    auth.token_env, env_file=self.env_file, secret=True
                )
        except ValueError:
            raise MCPError("MCP_CREDENTIAL_MISSING: 请在执行环境配置 MCP 凭据。") from None

        def http_client_factory(headers=None, timeout=None, auth=None):
            """复用配置中的超时和代理规则，关闭自动重定向。"""
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout or self.limits.timeout_seconds,
                auth=auth,
                follow_redirects=False,
                trust_env=self.server.policy.egress == "external",
            )

        return MultiServerMCPClient(
            {
                self.alias: {
                    "transport": "streamable_http",
                    "url": url,
                    "headers": headers,
                    "timeout": self.limits.timeout_seconds,
                    "sse_read_timeout": self.limits.timeout_seconds,
                    "httpx_client_factory": http_client_factory,
                }
            }
        )

    async def _manifest(self, session, initialized, url: str) -> MCPManifest:
        """完整读取有界分页目录；任何一页失败都不产出部分清单。"""
        found = {}
        cursor = None
        cursors = set()
        size = 0
        for _ in range(self.limits.catalog_max_pages):
            page = await session.list_tools(cursor=cursor)
            size += len(page.model_dump_json().encode())
            if size > self.limits.catalog_max_bytes:
                raise MCPError("MCP_CATALOG_TOO_LARGE: 工具目录超过配置的大小限制。")
            for item in page.tools:
                if item.name in found:
                    raise MCPError("MCP_CATALOG_INVALID: 远端工具名称重复。")
                found[item.name] = MCPToolDefinition.model_validate(
                    item.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
            cursor = page.nextCursor
            if cursor is None:
                break
            if cursor in cursors:
                raise MCPError("MCP_CATALOG_INVALID: 工具目录分页重复。")
            cursors.add(cursor)
        else:
            raise MCPError("MCP_CATALOG_TOO_LARGE: 工具目录超过配置的分页限制。")
        return MCPManifest(
            server=self.alias,
            endpoint=url,
            protocol_version=initialized.protocolVersion,
            server_info=initialized.serverInfo.model_dump(mode="json", exclude_none=True),
            tools=tuple(found[name] for name in sorted(found)),
        )

    async def discover(self) -> MCPManifest:
        """仅列举工具，供维护命令导入；不调用远端业务工具。"""
        try:
            url = endpoint(self.server, env_file=self.env_file)
            async with asyncio.timeout(self.limits.timeout_seconds):
                async with self._client(url).session(self.alias, auto_initialize=False) as session:
                    initialized = await session.initialize()
                    return await self._manifest(session, initialized, url)
        except Exception as error:
            raise mapped_error(error) from None

    async def call(self, entry: MCPEntry, arguments: dict) -> types.CallToolResult:
        """同 session 核对固定定义并调用；取消或超时会关闭本次连接。"""
        try:
            url = endpoint(self.server, env_file=self.env_file)
            if url != entry.endpoint:
                raise MCPError("MCP_RELEASE_CHANGED: MCP 地址变化，请重新装配发布。")
            async with asyncio.timeout(self.limits.timeout_seconds):
                async with self._client(url).session(self.alias, auto_initialize=False) as session:
                    initialized = await session.initialize()
                    current = await self._manifest(session, initialized, url)
                    expected_info = entry.manifest.server_info
                    if current.server_info.get("name") != expected_info.get("name") or (
                        self.server.pin_server_version
                        and current.server_info.get("version") != expected_info.get("version")
                    ):
                        raise MCPError("MCP_CONTRACT_CHANGED: MCP 服务身份变化，请重新导入。")
                    tool = next(
                        (item for item in current.tools if item.name == entry.definition.name), None
                    )
                    if tool is None or configuration_fingerprint(tool) != configuration_fingerprint(
                        entry.definition
                    ):
                        raise MCPError("MCP_CONTRACT_CHANGED: 工具定义变化，请重新导入。")
                    validate_definition(tool)
                    result = await session.send_request(
                        types.ClientRequest(
                            types.CallToolRequest(
                                params=types.CallToolRequestParams(
                                    name=tool.name, arguments=arguments
                                )
                            )
                        ),
                        types.CallToolResult,
                    )
                    if len(result.model_dump_json().encode()) > self.limits.result_max_bytes:
                        raise MCPError("MCP_RESULT_TOO_LARGE: MCP 原始结果超过配置的大小限制。")
                    return result
        except Exception as error:
            raise mapped_error(error) from None
