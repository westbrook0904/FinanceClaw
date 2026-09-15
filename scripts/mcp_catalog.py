"""维护 MCP 工具契约：列举、原子导入和离线检查，不调用远端业务工具。"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from financeclaw.kernel.mcp import MCPManifest
from financeclaw.shared.mcp.configuration import (
    MCPConfiguration,
    MCPRelease,
    manifest_json,
    validate_definition,
)


def write_manifest(path: Path, manifest: MCPManifest) -> None:
    """同目录写临时文件并替换，失败不破坏原有工具定义。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(manifest_json(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run(args) -> None:
    """离线检查不导入传输模块；维护动作仅进行 initialize/tools/list。"""
    if args.action == "check":
        release = MCPRelease(args.config, env_file=args.env_file)
        known = {"finance_agent", "market_research_agent", "ziwei_doushu_agent"}
        if set(release.configuration.agents) - known:
            raise ValueError("unknown MCP Agent binding")
        print(f"MCP 配置检查通过：{len(release.entries)} 个已启用工具。")
        return
    if not args.server:
        raise ValueError("discover/import requires --server")
    configuration = MCPConfiguration.from_file(args.config)
    server = configuration.servers.get(args.server)
    if server is None:
        raise ValueError("unknown MCP server alias")
    from financeclaw.agent_server.tools.mcp_transport import MCPTransport

    manifest = await MCPTransport(
        args.server, server, configuration.limits(server), env_file=args.env_file
    ).discover()
    if args.action == "discover":
        print(manifest_json(manifest), end="")
        return
    found = {tool.name: tool for tool in manifest.tools}
    if set(server.allowed_tools) - found.keys():
        raise ValueError("allowed tool is absent from the remote catalog")
    selected = tuple(found[name] for name in sorted(server.allowed_tools))
    for tool in selected:
        validate_definition(tool)
    manifest = manifest.model_copy(update={"tools": selected})
    if len(manifest_json(manifest).encode()) > configuration.limits(server).catalog_max_bytes:
        raise ValueError("imported manifest exceeds configured size")
    path = Path(args.config).resolve().parent / server.contracts
    write_manifest(path, manifest)
    print(f"已导入 {len(selected)} 个工具：{path}")
    if not server.enabled:
        print("服务仍为禁用状态；审阅契约后将 enabled 改为 true。")


def main() -> int:
    """解析维护命令并报告不含认证值的错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("discover", "import", "check"))
    parser.add_argument("--server")
    parser.add_argument("--config", default="config/mcp.toml")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        # 维护命令不打印 SDK traceback 或 Pydantic 的原始输入。
        from financeclaw.agent_server.tools.mcp_errors import MCPError, MCPUnavailableError

        if isinstance(error, MCPError | MCPUnavailableError):
            detail = str(error)
        elif isinstance(error, ValidationError):
            fields = error.errors(include_input=False, include_context=False, include_url=False)
            detail = "配置/契约字段错误：" + "; ".join(
                f"{'.'.join(map(str, item['loc'])) or '$'} ({item['type']})" for item in fields[:8]
            )
        elif isinstance(error, SchemaError):
            detail = "JSON Schema 无效：" + ".".join(map(str, error.path))
        elif isinstance(error, OSError):
            detail = f"无法读取或写入文件：{error.filename}（errno={error.errno}）"
        elif isinstance(error, ValueError):
            detail = str(error)
        else:
            detail = type(error).__name__
        print(f"MCP 检查/导入失败：{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
