"""通用 MCP 的发布声明；只保存连接引用、工具定义和平台读取策略。"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel.context import DataClassification

Alias = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$")]


class MCPDeclaration(BaseModel):
    """拒绝未知配置字段，错误中不回显可能误填的凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class MCPDefaults(MCPDeclaration):
    """共享连接和协议负载上限；服务可以覆盖，不改变 Agent 的调用额度。"""

    timeout_seconds: float = Field(default=30, gt=0, le=300)
    result_max_bytes: int = Field(default=2_097_152, ge=4096, le=16_777_216)
    catalog_max_bytes: int = Field(default=2_097_152, ge=4096, le=16_777_216)
    catalog_max_pages: int = Field(default=20, ge=1, le=100)


class MCPAuth(MCPDeclaration):
    """支持匿名、Bearer 或由环境变量提供的固定 Header；不保存密钥值。"""

    type: Literal["none", "bearer", "headers"] = "none"
    token_env: EnvName | None = None
    headers_env: dict[str, EnvName] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode(self):
        """每种认证方式只接受对应的引用字段，避免误配置被静默忽略。"""
        if self.type == "bearer":
            if not self.token_env or self.headers_env:
                raise ValueError("bearer auth requires token_env only")
        elif self.type == "headers":
            if not self.headers_env or self.token_env:
                raise ValueError("headers auth requires headers_env only")
        elif self.token_env or self.headers_env:
            raise ValueError("anonymous auth cannot contain credentials")
        for name in self.headers_env:
            if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9a-zA-Z]+", name):
                raise ValueError("invalid MCP authentication header name")
            if name.lower() in {"host", "content-length", "accept", "content-type"}:
                raise ValueError("MCP auth cannot override transport headers")
        return self


class MCPReadPolicy(MCPDeclaration):
    """首期只发布只读工具；交易工具须另行接入审批与持久恢复。"""

    side_effect: Literal["read"]
    required_scopes: frozenset[str] = Field(min_length=1)
    egress: Literal["external", "internal"] = "external"
    allowed_data_classes: frozenset[DataClassification] = Field(min_length=1)
    tenant_allowlist: frozenset[str] | None = None

    @model_validator(mode="after")
    def validate_scopes(self):
        """工具声明使用具体作用域，不通过通配符扩大平台权限。"""
        if any(not scope.strip() or scope == "*" for scope in self.required_scopes):
            raise ValueError("MCP policy requires explicit scopes")
        return self


class MCPResultView(MCPDeclaration):
    """可选的业务结果目录与预览策略，不改变原始回包或可读字段。"""

    delivery: Literal["auto", "reference"] = "auto"
    collection_path: str | None = Field(default=None, max_length=1024)
    preview_fields: tuple[str, ...] = Field(default=(), max_length=24)
    preview_records: int = Field(default=3, ge=0, le=3)

    @model_validator(mode="after")
    def validate_paths(self):
        """仅检查路径的协议写法，不推测业务数据是否存在。"""
        for path in (*self.preview_fields, self.collection_path):
            if path is not None and (
                len(path) > 1024
                or (path and not path.startswith("/"))
                or re.search(r"~(?![01])", path)
            ):
                raise ValueError("result view paths must be JSON pointers")
        return self


class MCPServer(MCPDeclaration):
    """一个 HTTP 服务的连接及允许清单；禁用时不装载契约或读取凭据。"""

    enabled: bool = False
    transport: Literal["streamable_http"] = "streamable_http"
    url: str | None = None
    url_env: EnvName | None = None
    allowed_hosts: frozenset[str] = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    contracts: str
    auth: MCPAuth = Field(default_factory=MCPAuth)
    policy: MCPReadPolicy
    tool_policies: dict[str, MCPReadPolicy] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)
    result_views: dict[str, MCPResultView] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    result_max_bytes: int | None = Field(default=None, ge=4096, le=16_777_216)
    catalog_max_bytes: int | None = Field(default=None, ge=4096, le=16_777_216)
    catalog_max_pages: int | None = Field(default=None, ge=1, le=100)
    pin_server_version: bool = False

    @model_validator(mode="after")
    def validate_connection(self):
        """确定地址来源、允许工具和逐工具覆盖，拒绝拼写错误与重复项。"""
        if bool(self.url) == bool(self.url_env):
            raise ValueError("MCP server requires exactly one of url or url_env")
        if not self.allowed_tools or len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("MCP allowed_tools must be nonempty and unique")
        if any(not name.strip() for name in self.allowed_tools):
            raise ValueError("empty MCP tool name")
        if (self.tool_policies.keys() | self.aliases.keys() | self.result_views.keys()) - set(
            self.allowed_tools
        ):
            raise ValueError("MCP tool overrides must reference allowed_tools")
        if any(item.egress != self.policy.egress for item in self.tool_policies.values()):
            raise ValueError("tools sharing a connection must have the same egress")
        return self


class MCPAgentBinding(MCPDeclaration):
    """Agent 可使用的 server.tool 引用，装配为已有 ToolRef。"""

    mcp_tools: tuple[str, ...] = ()


class MCPToolDefinition(BaseModel):
    """导入的远端工具原始定义，保留扩展字段，Schema 不转换成手写参数类。"""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] | None = Field(default=None, alias="outputSchema")


class MCPManifest(MCPDeclaration):
    """一次完整导入的固定工具清单；不包含认证值或业务查询结果。"""

    format_version: Literal[1] = 1
    server: str
    endpoint: str
    protocol_version: str
    server_info: dict[str, Any]
    tools: tuple[MCPToolDefinition, ...]
