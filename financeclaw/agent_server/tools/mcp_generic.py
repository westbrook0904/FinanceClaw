"""将固定 MCP 定义装配为普通 ManagedTool；业务结果仍回到根 Agent。"""

import asyncio
import json
from datetime import UTC, datetime

from jsonschema import Draft202012Validator
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.mcp_errors import MCPError
from financeclaw.agent_server.tools.mcp_transport import MCPTransport
from financeclaw.agent_server.tools.policy import ToolDecisionType, ToolPolicy
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.artifacts.views import mcp_view
from financeclaw.shared.mcp.configuration import MCPEntry, MCPRelease
from financeclaw.shared.releases.fingerprint import configuration_fingerprint


def managed_mcp_tool(entry: MCPEntry, *, env_file=None, transport=None) -> ManagedTool:
    """基于远端 JSON Schema 创建通用工具，运行身份由原生 ToolNode 注入。"""
    remote = transport or MCPTransport(
        entry.server_name, entry.server, entry.limits, env_file=env_file
    )
    validator = Draft202012Validator(entry.definition.input_schema)

    async def invoke(_financeclaw_runtime: ToolRuntime, **arguments):
        """复验可信身份与固定参数结构，原始结果交由现有工件中间件保存。"""
        runtime = _financeclaw_runtime
        context = ExecutionContext.model_validate(runtime.context)

        def receipt(content, *, error=False, artifact=None):
            """始终回应当前调用 ID，错误也能继续进入根模型循环。"""
            return ToolMessage(
                content=content,
                name=entry.governance.tool_id,
                tool_call_id=runtime.tool_call_id,
                status="error" if error else "success",
                artifact=artifact,
            )

        decision = ToolPolicy().evaluate(context, entry.governance, arguments)
        if decision.effect != ToolDecisionType.ALLOW:
            return receipt("MCP_ACCESS_DENIED: 当前身份无权使用此工具。", error=True)
        errors = list(validator.iter_errors(arguments))
        if errors:
            paths = set()
            for item in errors:
                prefix = ".".join(map(str, item.path))
                if item.validator == "required" and isinstance(item.instance, dict):
                    paths.update(
                        ".".join(filter(None, (prefix, name)))
                        for name in item.validator_value
                        if name not in item.instance
                    )
                else:
                    paths.add(f"{prefix or '$'} ({item.validator})")
            return receipt(
                "MCP_INPUT_INVALID: 参数不符合工具定义，请检查字段 " + ", ".join(sorted(paths)[:8]),
                error=True,
            )
        try:
            result = await remote.call(entry, arguments)
        except MCPError as error:
            return receipt(str(error), error=True)
        raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        source = {
            "server": entry.server_name,
            "tool": entry.definition.name,
            "queried_at": datetime.now(UTC).isoformat(),
            "contract_hash": configuration_fingerprint(entry.definition),
        }
        artifact = {"mcp": source, "arguments": arguments, "response": raw}
        schema = entry.definition.output_schema
        if not result.isError and schema is not None:
            if result.structuredContent is None or not Draft202012Validator(schema).is_valid(
                result.structuredContent
            ):
                return receipt(
                    "MCP_OUTPUT_INVALID: 结果不符合已导入的输出定义。",
                    error=True,
                    artifact=artifact,
                )
        _, data, _, _ = mcp_view(artifact)
        payload = {**source, "result": data}
        return receipt(
            json.dumps(payload, ensure_ascii=False), error=result.isError, artifact=artifact
        )

    def invoke_sync(_financeclaw_runtime: ToolRuntime, **arguments):
        """供同步调用方复用同一异步 SDK 链路，不创建另一套执行逻辑。"""
        return asyncio.run(invoke(_financeclaw_runtime, **arguments))

    return ManagedTool(
        StructuredTool(
            name=entry.governance.tool_id,
            description=entry.definition.description or entry.definition.name,
            args_schema=entry.definition.input_schema,
            func=invoke_sync,
            coroutine=invoke,
            metadata={
                "mcp_server": entry.server_name,
                "mcp_tool": entry.definition.name,
                "result_view": (
                    entry.server.result_views[entry.definition.name].model_dump()
                    if entry.definition.name in entry.server.result_views
                    else {}
                ),
            },
        ),
        entry.governance,
    )


def generic_mcp_tools(release: MCPRelease, *, env_file=None) -> tuple[ManagedTool, ...]:
    """构建启用服务的后端目录；每个 Agent 的权限绑定由发布装配处理。"""
    return tuple(managed_mcp_tool(entry, env_file=env_file) for entry in release.entries.values())
