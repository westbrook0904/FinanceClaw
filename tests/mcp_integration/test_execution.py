"""通过真实 MCP SDK、ToolNode 和根 Agent 验证通用工具链路。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import ClassVar

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.tools.mcp_generic import managed_mcp_tool
from financeclaw.agent_server.tools.mcp_transport import MCPTransport
from financeclaw.agent_server.tools.policy import TransientToolError
from financeclaw.kernel.context import DataClassification
from financeclaw.kernel.mcp import MCPAuth
from financeclaw.shared.mcp.configuration import MCPRelease
from financeclaw.shared.mcp.contracts import input_structure
from financeclaw.shared.turns.snapshots import agent_snapshot
from scripts.mcp_catalog import run
from tests.turn_support import seed_execution


def seed_root(components, context, thread_id, message="查询酒店"):
    """为真实产品图固定根发布、业务 Turn 和当前原生输入标识。"""
    snapshot = agent_snapshot(
        components.default_agent_profile, context, thread_id=thread_id, input_hash="synthetic"
    )
    snapshot["user_message_id"] = "mcp-input"
    return seed_execution(
        components.conversation_repository.execution, context, snapshot, message=message
    )


async def invoke(settings, context, *, arguments=None, transport=None):
    """直接 ToolCall 入口也必须携带可信身份和准确调用 ID。"""
    entry = settings.mcp_release.entries["hotel.search"]
    tool = managed_mcp_tool(entry, env_file=None, transport=transport).tool
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="mcp-call",
        store=None,
    )
    return await tool.ainvoke(
        {
            "name": tool.name,
            "type": "tool_call",
            "id": "mcp-call",
            "args": {
                **({"query": "Shanghai"} if arguments is None else arguments),
                "_financeclaw_runtime": runtime,
            },
        }
    )


@pytest.mark.asyncio
async def test_sdk_session_pagination_headers_and_raw_results(settings, context, protocol):
    """标准 SDK 请求含认证及初始化，远端只收到业务参数，双通道原文保留。"""
    result = await invoke(settings, context)
    assert result.status == "success" and result.tool_call_id == "mcp-call"
    assert result.artifact["response"]["structuredContent"]["hotel_id"] == "hotel-1"
    assert result.artifact["response"]["content"][0]["text"] == "synthetic hotel"
    assert "synthetic-mcp-credential" not in result.model_dump_json()
    calls = [body for _, body in protocol.requests if body.get("method") == "tools/call"]
    assert calls[0]["params"] == {"name": "search", "arguments": {"query": "Shanghai"}}
    methods = [body.get("method") for _, body in protocol.requests]
    assert methods[:4] == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    for request, _ in protocol.requests:
        assert request.headers["authorization"] == "Bearer synthetic-mcp-credential"
        assert "application/json" in request.headers["accept"]
        assert "text/event-stream" in request.headers["accept"]


@pytest.mark.asyncio
async def test_structured_only_result_is_model_visible(settings, context, protocol):
    """只有 structuredContent 时，根仍获得 JSON，不只是不可见的 artifact。"""
    protocol.structured_only = True
    result = await invoke(settings, context)
    assert json.loads(result.content)["result"]["hotel_id"] == "hotel-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tool_metadata", "parameter_description", "output_schema"])
async def test_documentation_changes_do_not_block_business_call(
    settings, context, protocol, change
):
    """复现 RollingGo 字段文案更新：参数结构相同就真正发出 tools/call。"""
    if change == "tool_metadata":
        protocol.tools[0].update(description="Updated guidance", title="New title")
        protocol.tools[0]["annotations"] = {"title": "Display name"}
    elif change == "parameter_description":
        protocol.tools[0]["inputSchema"]["properties"]["query"].update(
            description="Use destination followed by city", title="Destination", examples=["x"]
        )
    else:
        protocol.tools[0]["outputSchema"] = {"type": "object"}
    result = await invoke(settings, context)
    assert result.status == "success"
    assert sum(body.get("method") == "tools/call" for _, body in protocol.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["type", "enum", "minLength", "property_name"])
async def test_parameter_structure_changes_still_block_before_call(
    settings, context, protocol, change
):
    """实际入参约束变化仍阻断，且说明改查询参数重试不能修复发布差异。"""
    schema = protocol.tools[0]["inputSchema"]
    if change == "property_name":
        schema["properties"]["destination"] = schema["properties"].pop("query")
    else:
        schema["properties"]["query"][change] = {
            "type": "integer",
            "enum": ["Shanghai"],
            "minLength": 2,
        }[change]
    result = await invoke(settings, context)
    assert result.status == "error" and "MCP_CONTRACT_CHANGED" in result.content
    assert "不要重试" in result.content
    assert not any(body.get("method") == "tools/call" for _, body in protocol.requests)


def test_annotation_removal_preserves_parameter_names_and_literal_values():
    """同名业务字段及常量不可被当成 Schema 文案剔除，嵌套说明可以更新。"""
    schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "old"},
            "settings": {"const": {"description": "business value", "default": 1}},
            "kind": {"enum": [{"title": "literal value"}]},
            "rows": {"type": "array", "items": {"$ref": "#/$defs/row"}},
        },
        "$defs": {"row": {"type": "object", "properties": {"title": {"type": "string"}}}},
        "required": ["description", "kind"],
    }
    documented = deepcopy(schema)
    documented["properties"]["description"]["description"] = "new"
    documented["$defs"]["row"]["properties"]["title"]["examples"] = ["sample"]
    documented["required"].reverse()
    assert input_structure(schema) == input_structure(documented)
    for key in ("description", "settings", "kind"):
        changed = deepcopy(schema)
        if key == "description":
            changed["properties"][key]["type"] = "number"
        elif key == "settings":
            changed["properties"][key]["const"]["description"] = "other value"
        else:
            changed["properties"][key]["enum"][0]["title"] = "other value"
        assert input_structure(schema) != input_structure(changed)
    assert schema["properties"]["description"]["description"] == "old"


def test_structure_comparison_retains_tuple_order_but_not_set_order():
    """枚举与联合类型的排序不影响参数，按位置定义的元组顺序仍影响输入。"""
    schema = {"type": ["string", "null"], "enum": ["a", "b", None]}
    reordered = {"type": ["null", "string"], "enum": [None, "b", "a"]}
    assert input_structure(schema) == input_structure(reordered)
    first = {"prefixItems": [{"type": "string"}, {"type": "number"}]}
    second = {"prefixItems": list(reversed(first["prefixItems"]))}
    assert input_structure(first) != input_structure(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scope", "tenant", "classification", "missing", "wrong_type"])
async def test_denied_or_invalid_input_never_contacts_mcp(settings, context, protocol, kind):
    """权限和协议参数错误在发送之前返回可修正回执，不替用户补值。"""
    arguments = None
    if kind == "scope":
        context = context.model_copy(update={"scopes": frozenset()})
    elif kind == "classification":
        context = context.model_copy(update={"data_classification": "restricted"})
    elif kind == "tenant":
        entry = settings.mcp_release.entries["hotel.search"]
        object.__setattr__(
            entry,
            "governance",
            entry.governance.model_copy(update={"tenant_allowlist": {"different"}}),
        )
    else:
        arguments = {} if kind == "missing" else {"query": 42}
    result = await invoke(settings, context, arguments=arguments)
    assert result.status == "error" and not protocol.requests
    if kind in {"missing", "wrong_type"}:
        assert "query" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_protocol_errors_have_stable_safe_classification(settings, context, protocol, status):
    """认证/限流不重试，瞬态服务错误交给已有 ToolRetry，原响应不公开。"""
    protocol.status = status
    if status == 503:
        with pytest.raises(TransientToolError, match="MCP_UNAVAILABLE"):
            await invoke(settings, context)
    else:
        result = await invoke(settings, context)
        assert result.status == "error" and str(status) in result.content
        assert "private upstream" not in result.content


@pytest.mark.asyncio
async def test_missing_credential_is_a_tool_error(settings, context, monkeypatch):
    """未配置密钥不影响离线装配，实际调用返回错误而不破坏消息批次。"""
    monkeypatch.delenv("TEST_MCP_KEY", raising=False)
    result = await invoke(settings, context)
    assert result.status == "error" and "MCP_CREDENTIAL_MISSING" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["schema", "removed", "remote_error", "output"])
async def test_remote_changes_and_errors_are_not_success(settings, context, protocol, change):
    """远端变化先拒绝，业务错误和不合约输出保留原始响应供回读。"""
    if change == "schema":
        protocol.tools[0]["inputSchema"]["required"].append("new_field")
    elif change == "removed":
        protocol.tools = protocol.tools[1:]
    elif change == "remote_error":
        protocol.is_error = True
    else:
        protocol.result_override = {"content": [], "structuredContent": {"unexpected": True}}
    result = await invoke(settings, context)
    assert result.status == "error"
    if change in {"schema", "removed"}:
        assert "MCP_CONTRACT_CHANGED" in result.content
        assert not any(body.get("method") == "tools/call" for _, body in protocol.requests)
    else:
        assert result.artifact["response"]


@pytest.mark.asyncio
async def test_cli_import_is_paginated_and_atomic(config_path, protocol):
    """完整导入可重新装配，后续分页失败不会覆盖原来可用的契约。"""
    args = SimpleNamespace(action="import", config=str(config_path), env_file=None, server="hotel")
    await run(args)
    path = config_path.parent / "hotel.json"
    before = path.read_bytes()
    assert len(MCPRelease(str(config_path), env_file=None).entries) == 4
    assert not any(body.get("method") == "tools/call" for _, body in protocol.requests)
    protocol.page_failure = True
    with pytest.raises(TransientToolError):
        await run(args)
    assert path.read_bytes() == before


class OrderedModel(OfflineFinanceModel):
    """只替换模型选择，真实根图顺序执行两个工具并作出最终回答。"""

    calls: ClassVar[list] = []

    def _generate(self, messages, *args, **kwargs):
        """记录首轮可见工具与回执，后一次调用使用前次返回的酒店标识。"""
        receipts = [item for item in messages if isinstance(item, ToolMessage)]
        type(self).calls.append((set(self._bound_tool_names), receipts))
        index = len(receipts)
        if index < 2:
            name, arguments = (
                ("mcp__hotel__search", {"query": "Shanghai"})
                if index == 0
                else (
                    "mcp__hotel__detail",
                    {"hotel_id": receipts[0].artifact["response"]["structuredContent"]["hotel_id"]},
                )
            )
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": arguments, "id": f"query-{index}", "type": "tool_call"}
                ],
            )
        else:
            message = AIMessage(content="完成两次实际工具查询后的根回答")
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
@pytest.mark.parametrize("ziwei_enabled", [False, True])
async def test_real_root_loop_injects_runtime_and_controls_completion(
    settings, context, protocol, config_path, ziwei_enabled
):
    """新工具首轮即出现，原生注入身份，工具结果后根继续调度再最终回答。"""
    if ziwei_enabled:
        settings.ziwei_enabled = True
        settings.ziwei_hmac_key = SecretStr("synthetic-ziwei-test-key-00000000")
        config_path.write_text(
            config_path.read_text().replace(
                'allowed_data_classes = ["public", "internal"]',
                'allowed_data_classes = ["public", "internal", "confidential"]',
                1,
            )
        )
        context = context.model_copy(
            update={"data_classification": DataClassification.CONFIDENTIAL}
        )
    components = build_components(settings, enable_persistence=True)
    try:
        context = seed_root(components, context, "mcp-root")
        OrderedModel.calls = []
        graph = components.agent_factory.build(
            components.default_agent_profile, model=OrderedModel()
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(id="mcp-input", content="查上海酒店并查看详情")]},
            config={"configurable": {"thread_id": "mcp-root"}},
            context=context,
        )
        assert len(OrderedModel.calls) == 3
        visible = OrderedModel.calls[0][0]
        assert {"mcp__hotel__search", "mcp__hotel__detail"}.issubset(visible)
        if ziwei_enabled:
            assert components.default_agent_profile.data_classification == "confidential"
        assert "mcp__other__search" not in visible and "search_tools" not in visible
        assert result["messages"][-1].content == "完成两次实际工具查询后的根回答"
        receipts = [item for item in result["messages"] if isinstance(item, ToolMessage)]
        assert [item.tool_call_id for item in receipts] == ["query-0", "query-1"]
        assert all(item.status == "success" for item in receipts)
    finally:
        components.database.close()


@pytest.mark.asyncio
async def test_large_raw_result_uses_existing_archive(settings, context, protocol):
    """真实数据库与归档中间件保存完整结构化结果，模型只拿可回读引用。"""
    from tests.stage6fix.test_batch_tools import BatchModel, call

    components = build_components(settings, enable_persistence=True)
    try:
        context = seed_root(components, context, "mcp-archive")
        protocol.result_override = {
            "content": [{"type": "text", "text": "large"}],
            "structuredContent": {"hotel_id": "hotel-1", "details": "数据" * 40000},
        }
        graph = components.agent_factory.build(
            components.default_agent_profile,
            model=BatchModel(calls=[call("mcp__hotel__search", 1, query="Shanghai")]),
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(id="mcp-input", content="查上海酒店")]},
            config={"configurable": {"thread_id": "mcp-archive"}},
            context=context,
        )
        message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
        assert len(message.content.encode()) < components.artifact_service.inline_bytes
        stored = json.loads(
            components.artifact_service.read(message.artifact["artifact_id"], context=context)
        )
        assert (
            stored["raw"]["artifact"]["response"]["structuredContent"]["details"] == "数据" * 40000
        )
        assert "content" not in stored["raw"]
    finally:
        components.database.close()


def test_root_final_model_allowance_is_reserved_in_sql(settings, context):
    """普通尝试不能占用预留额度，取消和硬上限仍然阻止最终请求。"""
    from financeclaw.shared.turns.types import ExecutionConflict
    from tests.turn_support import cancel_execution

    components = build_components(settings, enable_persistence=True)
    try:
        context = seed_root(components, context, "reserve-final")
        execution = components.conversation_repository.execution
        limit = execution.get(context.turn_id)["release_snapshot"]["limits"]["model"]
        for _ in range(limit - 1):
            execution.consume(context.turn_id, "model")
        with pytest.raises(ExecutionConflict, match="budget"):
            execution.consume(context.turn_id, "model")
        execution.consume(context.turn_id, "model", final_answer=True)
        assert execution.get(context.turn_id)["model_calls"] == limit
        with pytest.raises(ExecutionConflict, match="budget"):
            execution.consume(context.turn_id, "model", final_answer=True)
        cancel_execution(execution, context.turn_id)
        with pytest.raises(ExecutionConflict):
            execution.consume(context.turn_id, "model", final_answer=True)
    finally:
        components.database.close()


@pytest.mark.asyncio
async def test_persistent_root_finishes_at_tree_budget_and_records_empty_tools(settings, context):
    """真实 SQL 配额先于本地配额耗尽时，最后一次仍可回答且 Manifest 如实记录。"""
    from tests.stage6fix.test_finish_budget import BudgetModel

    components = build_components(settings, enable_persistence=True)
    try:
        profile = components.default_agent_profile.model_copy(update={"max_tree_model_calls": 3})
        snapshot = agent_snapshot(profile, context, thread_id="tree-budget", input_hash="test")
        snapshot["user_message_id"] = "budget-input"
        context = seed_execution(components.conversation_repository.execution, context, snapshot)
        BudgetModel.observed = []
        graph = components.agent_factory.build(profile, model=BudgetModel(batches=(1,)))
        result = await graph.ainvoke(
            {"messages": [HumanMessage(id="budget-input", content="比较已有结果")]},
            config={"configurable": {"thread_id": "tree-budget"}},
            context=context,
        )
        assert len(BudgetModel.observed) == 3 and not BudgetModel.observed[-1]
        assert "已有查询结果" in result["messages"][-1].content
        execution = components.conversation_repository.execution
        assert execution.get(context.turn_id)["model_calls"] == 3
        manifests = components.conversation_repository.list_manifests(context.conversation_id)
        assert len(manifests) == 3 and not manifests[-1].exposed_tools
    finally:
        components.database.close()


@pytest.mark.asyncio
async def test_cancel_propagates_out_of_tool(settings, context):
    """停止本轮时取消保持原生语义，不能被转换成成功或自动重试。"""
    entered = asyncio.Event()
    exited = asyncio.Event()

    class SlowRemote:
        """模拟仍在运行的查询，并记录取消清理。"""

        async def call(self, entry, arguments):
            """等待取消，不产生替代业务结果。"""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

    task = asyncio.create_task(invoke(settings, context, transport=SlowRemote()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_sdk_closes_http_session_on_cancel_and_timeout(settings, context, protocol, mode):
    """取消和总超时穿过真实 SDK 的 TaskGroup 后仍释放 HTTP session。"""
    entry = settings.mcp_release.entries["hotel.search"]
    remote = MCPTransport(
        entry.server_name,
        entry.server,
        entry.limits.model_copy(update={"timeout_seconds": 0.1 if mode == "timeout" else 5}),
        env_file=None,
    )
    protocol.wait_for_call = True
    task = asyncio.create_task(invoke(settings, context, transport=remote))
    await asyncio.wait_for(protocol.call_entered.wait(), 2)
    if mode == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else TransientToolError):
        await asyncio.wait_for(task, 2)
    assert protocol.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["catalog_max_pages", "catalog_max_bytes", "result_max_bytes"])
async def test_sdk_bounds_catalog_and_result(settings, context, protocol, limit):
    """远端无限目录或过大响应受连接级上限约束，不能冒充正常结果。"""
    entry = settings.mcp_release.entries["hotel.search"]
    if limit == "catalog_max_bytes":
        protocol.tools[0]["description"] = "description" * 1000
    elif limit == "result_max_bytes":
        protocol.result_override = {"content": [{"type": "text", "text": "large" * 1000}]}
    remote = MCPTransport(
        entry.server_name,
        entry.server,
        entry.limits.model_copy(update={limit: 1 if limit == "catalog_max_pages" else 4096}),
        env_file=None,
    )
    result = await invoke(settings, context, transport=remote)
    assert result.status == "error" and "TOO_LARGE" in result.content
    assert protocol.closed == 1
    calls = [body for _, body in protocol.requests if body.get("method") == "tools/call"]
    assert len(calls) == (1 if limit == "result_max_bytes" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 503])
async def test_root_batch_errors_keep_call_pairs_and_single_retry_layer(
    settings, context, protocol, status
):
    """批量读取失败也收齐回执；401 不重试，503 仅受既有两次重试策略管理。"""
    from tests.stage6fix.test_batch_tools import BatchModel, call

    protocol.status = status
    components = build_components(settings, enable_persistence=True)
    try:
        context = seed_root(components, context, "mcp-errors")
        graph = components.agent_factory.build(
            components.default_agent_profile,
            model=BatchModel(
                calls=[
                    call("mcp__hotel__search", 1, query="Shanghai"),
                    call("mcp__hotel__detail", 2, hotel_id="hotel-1"),
                ]
            ),
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(id="mcp-input", content="查询酒店")]},
            config={"configurable": {"thread_id": "mcp-errors"}},
            context=context,
        )
        receipts = [item for item in result["messages"] if isinstance(item, ToolMessage)]
        assert [item.tool_call_id for item in receipts] == ["call-1", "call-2"]
        assert all(item.status == "error" for item in receipts)
        initialized = [body for _, body in protocol.requests if body.get("method") == "initialize"]
        assert len(initialized) == (2 if status == 401 else 6)
        assert isinstance(result["messages"][-1], AIMessage)
    finally:
        components.database.close()


@pytest.mark.asyncio
async def test_explicit_tool_directive_and_progress_use_existing_path(settings, context, protocol):
    """JSON Schema 工具兼容现有 /tool 和原生进度事件，工具完成后仍由根回答。"""
    from tests.stage6fix.test_batch_tools import BatchModel, call

    components = build_components(settings, enable_persistence=True)
    try:
        message = '/tool mcp__hotel__search {"query":"Shanghai"}'
        context = seed_root(components, context, "mcp-directive", message)
        graph = components.agent_factory.build(
            components.default_agent_profile,
            model=BatchModel(calls=[call("mcp__hotel__search", 1, query="Shanghai")]),
        )
        chunks = [
            item
            async for item in graph.astream(
                {"messages": [HumanMessage(id="mcp-input", content=message)]},
                config={"configurable": {"thread_id": "mcp-directive"}},
                context=context,
                stream_mode=["custom", "values"],
            )
        ]
        events = [payload for mode, payload in chunks if mode == "custom" and "tool" in payload]
        assert [event["status"] for event in events] == ["started", "completed"]
        assert all(event["tool"] == "mcp__hotel__search" for event in events)
        final = [payload for mode, payload in chunks if mode == "values"][-1]
        receipt = next(item for item in final["messages"] if isinstance(item, ToolMessage))
        assert receipt.status == "success"
        assert isinstance(final["messages"][-1], AIMessage)
    finally:
        components.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["none", "headers"])
async def test_generic_auth_modes_only_send_configured_headers(settings, context, protocol, auth):
    """匿名与 Header 认证复用同一 SDK，不隐式携带服务原来配置的 Bearer。"""
    entry = settings.mcp_release.entries["hotel.search"]
    server = entry.server.model_copy(
        update={
            "auth": MCPAuth(
                type=auth, headers_env={"X-Api-Key": "TEST_MCP_KEY"} if auth == "headers" else {}
            )
        }
    )
    result = await invoke(
        settings,
        context,
        transport=MCPTransport(entry.server_name, server, entry.limits, env_file=None),
    )
    assert result.status == "success"
    for request, _ in protocol.requests:
        assert "authorization" not in request.headers
        assert ("x-api-key" in request.headers) == (auth == "headers")


@pytest.mark.asyncio
async def test_redirect_is_not_followed(settings, context, protocol):
    """端点重定向直接失败，不让客户端把认证带到第二个地址。"""
    protocol.status = 302
    result = await invoke(settings, context)
    assert result.status == "error" and "302" in result.content
    assert len(protocol.requests) == 1


class MCPResearchModel(OfflineFinanceModel):
    """子 Agent 选择自己的 MCP，再按既有 Worker 结果协议向根交付。"""

    visible: ClassVar[set] = set()

    def _generate(self, messages, *args, **kwargs):
        """模型仅负责确定性选择，查询和父子图授权均走真实代码。"""
        type(self).visible = set(self._bound_tool_names)
        if not any(isinstance(item, ToolMessage) for item in messages):
            name, arguments = "mcp__other__search", {"query": "child-specific-query"}
        else:
            name, arguments = (
                "MarketResearchResult",
                {
                    "outcome": "success",
                    "summary": "bounded MCP research",
                    "evidence": [
                        {"provider": "synthetic-mcp", "as_of": "2026-09-15", "summary": "fixture"}
                    ],
                },
            )
        message = AIMessage(content="", tool_calls=[{"id": name, "name": name, "args": arguments}])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
async def test_child_binding_executes_with_narrowed_root_grant(
    settings, config_path, context, protocol
):
    """第二个服务仅绑定 Worker，根通过中央委派获取其结果而不能直调叶子。"""
    from tests.stage6fix.test_batch_tools import BatchModel, call

    config_path.write_text(
        config_path.read_text() + '\n[agents.market_research_agent]\nmcp_tools = ["other.search"]\n'
    )
    components = build_components(settings, enable_persistence=True)
    try:
        context = context.model_copy(update={"scopes": context.scopes | {"market:read"}})
        context = seed_root(components, context, "mcp-child")
        child = components.tool_catalog.resolve("call_agent__market_research_agent").tool
        child.graph = components.agent_factory.build(
            child.release, model=MCPResearchModel(), fallback_models=(), checkpointer=None
        )
        graph = components.agent_factory.build(
            components.default_agent_profile,
            model=BatchModel(
                calls=[call("call_agent__market_research_agent", 1, task="read MCP evidence")]
            ),
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(id="mcp-input", content="查询酒店")]},
            config={"configurable": {"thread_id": "mcp-child"}},
            context=context,
        )
        assert "mcp__other__search" in MCPResearchModel.visible
        assert "mcp__hotel__search" not in MCPResearchModel.visible
        assert "mcp__other__search" not in {
            ref.tool_id for ref in components.default_agent_profile.allowed_tools
        }
        receipt = next(item for item in result["messages"] if isinstance(item, ToolMessage))
        assert json.loads(receipt.content)["outcome"] == "success"
        calls = [body for _, body in protocol.requests if body.get("method") == "tools/call"]
        assert calls[0]["params"]["arguments"] == {"query": "child-specific-query"}
    finally:
        components.database.close()
