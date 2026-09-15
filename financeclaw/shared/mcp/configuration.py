"""从 TOML 和已导入工具定义构造 API/Worker 一致的 MCP 发布。"""

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from pydantic import Field, SecretStr, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict

from financeclaw.kernel.agents import ToolRef
from financeclaw.kernel.mcp import (
    Alias,
    MCPAgentBinding,
    MCPDeclaration,
    MCPDefaults,
    MCPManifest,
    MCPServer,
    MCPToolDefinition,
)
from financeclaw.kernel.tools import ToolGovernance
from financeclaw.shared.infrastructure.security.egress import EgressPolicy
from financeclaw.shared.releases.fingerprint import configuration_fingerprint


def environment_value(name: str, *, env_file=None, secret=False):
    """按环境变量优先、dotenv 次之解析引用，错误不包含真实值。"""
    settings = create_model(
        "MCPCredential" if secret else "MCPEndpoint",
        __base__=BaseSettings,
        __config__=SettingsConfigDict(extra="ignore", hide_input_in_errors=True),
        value=(SecretStr, Field(validation_alias=name)),
    )(_env_file=env_file)
    value = settings.value.get_secret_value()
    if not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"empty or invalid MCP environment variable: {name}")
    return value


def endpoint(server: MCPServer, *, env_file=None) -> str:
    """解析非密钥端点并复用出站策略，拒绝 URL 内嵌认证或查询串。"""
    value = server.url or environment_value(server.url_env, env_file=env_file)
    parts = urlsplit(value)
    if parts.query or parts.fragment:
        raise ValueError("MCP endpoint cannot contain query parameters or fragments")
    return EgressPolicy(
        server.allowed_hosts,
        require_https=server.policy.egress == "external",
        allow_private_hosts=False,
    ).validate(value)


def validate_definition(tool: MCPToolDefinition) -> None:
    """检查 JSON Schema 及注入参数冲突，只验证协议结构，不补业务参数。"""
    if tool.input_schema.get("type") != "object":
        raise ValueError(f"MCP inputSchema must be an object: {tool.name}")
    if "_financeclaw_runtime" in tool.input_schema.get("properties", {}):
        raise ValueError("MCP input uses a reserved runtime parameter")
    for schema in (tool.input_schema, tool.output_schema):
        if schema is not None:
            Draft202012Validator.check_schema(schema)
            _local_references(schema)


def _local_references(value):
    """JSON Schema 引用只在同一文档内解析，校验时不另行联网。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and not str(item).startswith("#"):
                raise ValueError("MCP schemas must use local references")
            _local_references(item)
    elif isinstance(value, list):
        for item in value:
            _local_references(item)


class MCPConfiguration(MCPDeclaration):
    """通用配置入口；关闭的服务可先配置，导入成功后再启用。"""

    defaults: MCPDefaults = Field(default_factory=MCPDefaults)
    servers: dict[Alias, MCPServer] = Field(default_factory=dict)
    agents: dict[Alias, MCPAgentBinding] = Field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str):
        """读取显式配置文件，错误配置不静默降级为空目录。"""
        with Path(path).open("rb") as stream:
            configuration = cls.model_validate(tomllib.load(stream))
        for binding in configuration.agents.values():
            if len(set(binding.mcp_tools)) != len(binding.mcp_tools):
                raise ValueError("duplicate MCP Agent binding")
            for ref in binding.mcp_tools:
                name, separator, tool = ref.partition(".")
                server = configuration.servers.get(name)
                if not separator or server is None or tool not in server.allowed_tools:
                    raise ValueError(f"unknown MCP Agent binding: {ref}")
        return configuration

    def limits(self, server: MCPServer) -> MCPDefaults:
        """将服务覆盖合并到默认连接参数。"""
        return MCPDefaults.model_validate(
            {
                name: getattr(server, name) or value
                for name, value in self.defaults.model_dump().items()
            }
        )


@dataclass(frozen=True)
class MCPEntry:
    """一个工具的固定连接、协议定义和治理声明；没有执行对象或密钥。"""

    server_name: str
    server: MCPServer
    endpoint: str
    limits: MCPDefaults
    manifest: MCPManifest
    definition: MCPToolDefinition
    governance: ToolGovernance

    def release(self) -> dict[str, Any]:
        """把契约、治理和非密钥连接纳入使用该工具的 Agent 发布指纹。"""
        return {
            "server": self.server_name,
            "endpoint": self.endpoint,
            "auth": self.server.auth.model_dump(),
            "allowed_hosts": sorted(self.server.allowed_hosts),
            "limits": self.limits.model_dump(),
            "pin_server_version": self.server.pin_server_version,
            "server_info": self.manifest.server_info,
            "protocol_version": self.manifest.protocol_version,
            "tool": self.definition.model_dump(by_alias=True, exclude_none=True),
            "governance": self.governance,
        }


class MCPRelease:
    """启动时固定的通用 MCP 目录，各 Agent 仅获得显式绑定的工具。"""

    def __init__(self, path: str, *, env_file=None):
        """从本地文件装配启用服务，解析端点但不读取认证或访问远端。"""
        self.configuration = MCPConfiguration.from_file(path)
        entries = {}
        names = set()
        for alias, server in self.configuration.servers.items():
            if not server.enabled:
                continue
            url = endpoint(server, env_file=env_file)
            limits = self.configuration.limits(server)
            source = Path(path).resolve().parent / server.contracts
            raw = source.read_bytes()
            if len(raw) > limits.catalog_max_bytes:
                raise ValueError(f"MCP manifest exceeds configured size: {alias}")
            manifest = MCPManifest.model_validate_json(raw)
            if manifest.server != alias or manifest.endpoint != url:
                raise ValueError(f"MCP manifest belongs to another endpoint: {alias}")
            remote = {tool.name: tool for tool in manifest.tools}
            if len(remote) != len(manifest.tools):
                raise ValueError(f"duplicate MCP tool definitions: {alias}")
            for tool_name in server.allowed_tools:
                if tool_name not in remote:
                    raise ValueError(
                        f"MCP tool missing from imported manifest: {alias}.{tool_name}"
                    )
                definition = remote[tool_name]
                validate_definition(definition)
                name = server.aliases.get(tool_name, f"mcp__{alias}__{tool_name}")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name):
                    raise ValueError(f"set a short valid MCP tool alias: {alias}.{tool_name}")
                if name in names:
                    raise ValueError(f"duplicate MCP model tool name: {name}")
                names.add(name)
                policy = server.tool_policies.get(tool_name, server.policy)
                governance = ToolGovernance(
                    tool_id=name,
                    version="1.0.0",
                    idempotency="idempotent",
                    risk_level="low",
                    approval="none",
                    sensitivity="internal",
                    retry_profile="transient_read",
                    **policy.model_dump(),
                )
                entries[f"{alias}.{tool_name}"] = MCPEntry(
                    alias, server, url, limits, manifest, definition, governance
                )
        self.entries = MappingProxyType(entries)

    def for_agent(self, agent_id: str) -> tuple[MCPEntry, ...]:
        """解析 Agent 的启用工具，禁用服务的绑定不参与发布。"""
        binding = self.configuration.agents.get(agent_id, MCPAgentBinding())
        return tuple(self.entries[ref] for ref in binding.mcp_tools if ref in self.entries)

    def refs(self, agent_id: str) -> tuple[ToolRef, ...]:
        """将配置别名编译为已有版本化 ToolRef。"""
        return tuple(
            ToolRef(tool_id=item.governance.tool_id, version=item.governance.version)
            for item in self.for_agent(agent_id)
        )

    def fingerprint(self, agent_id: str) -> str:
        """只对本 Agent 可达的 MCP 配置求摘要，不含其他服务或真实密钥。"""
        return configuration_fingerprint([item.release() for item in self.for_agent(agent_id)])


def manifest_json(manifest: MCPManifest) -> str:
    """输出稳定可审阅的导入文件，保留服务原始 Schema 和扩展字段。"""
    return (
        json.dumps(
            manifest.model_dump(mode="json", by_alias=True, exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
