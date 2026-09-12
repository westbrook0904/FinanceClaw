"""真实 BaseTool 与 ArtifactService 的结果、权限和大小边界。"""

import json

import pytest
from langchain.tools import ToolRuntime

from financeclaw.agent_server.tools.taibu import taibu_tools
from financeclaw.kernel.context import DataClassification
from financeclaw.shared.artifacts.repository import ArtifactNotFound
from tests.taibu.conftest import BAZI


async def invoke(stack, name="taibu_bazi", arguments=None, context=None):
    """通过真实 ToolCall 入口调用，包含由运行时注入的调用身份。"""
    tool = next(
        t.tool
        for t in taibu_tools(stack.settings, stack.artifacts, client=stack.remote)
        if t.tool.name == name
    )
    runtime = ToolRuntime(
        state={},
        context=context or stack.context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="call-taibu",
        store=None,
    )
    return await tool.ainvoke(
        {
            "name": name,
            "id": "call-taibu",
            "type": "tool_call",
            "args": {**(BAZI if arguments is None else arguments), "runtime": runtime},
        }
    )


@pytest.mark.asyncio
async def test_structured_results_and_warnings_are_archived(stack):
    """程序不解析 Markdown，双通道原文完整保存而模型消息只含小投影。"""
    result = await invoke(stack)
    envelope = json.loads(result.content)
    assert result.status == "success" and len(envelope["data"]["四柱"]) == 4
    assert envelope["warnings"] and envelope["convention"]["solar_time"] == "standard"
    raw = json.loads(
        stack.artifacts.read(envelope["artifact_ref"]["artifact_id"], context=stack.context)
    )
    assert raw["response"] == stack.remote.results["bazi"]
    assert raw["arguments"]["birthMinute"] == 0
    assert "response" not in envelope and result.artifact == envelope["artifact_ref"]
    with pytest.raises(ArtifactNotFound):
        stack.artifacts.read(
            result.artifact["artifact_id"],
            context=stack.context.model_copy(update={"tenant_id": "other"}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"scopes": frozenset()},
        {"data_classification": DataClassification.INTERNAL},
        {"tenant_id": "other"},
    ],
)
async def test_denied_call_does_not_contact_server(stack, change):
    """直接工具入口也复验 scope、租户与资料密级。"""
    stack.settings.taibu_tenant_allowlist = frozenset({stack.context.tenant_id})
    result = await invoke(stack, context=stack.context.model_copy(update=change))
    assert result.status == "error" and "TAIBU_ACCESS_DENIED" in result.content
    assert not stack.remote.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["missing_json", "empty_json", "missing_pillar", "wrong_gender", "remote_error"]
)
async def test_invalid_success_is_error_and_original_is_retained(stack, kind):
    """不完整盘面与上游 isError 不能被误记为成功，诊断原文仍可回读。"""
    raw = stack.remote.results["bazi"]
    if kind == "missing_json":
        raw.pop("structuredContent")
    elif kind == "empty_json":
        raw["structuredContent"] = {}
    elif kind == "missing_pillar":
        raw["structuredContent"]["四柱"].pop()
    elif kind == "wrong_gender":
        raw["structuredContent"]["基本信息"]["性别"] = "女"
    else:
        raw["isError"] = True
    result = await invoke(stack)
    value = json.loads(result.content)
    assert value["outcome"] == "error" and result.status == "error" and not value["data"]
    assert stack.artifacts.read(result.artifact["artifact_id"], context=stack.context)


@pytest.mark.asyncio
async def test_unresolved_true_solar_time_is_not_success(stack):
    """有四柱但所要求的校正未完成时仍返回明确错误。"""
    result = await invoke(stack, arguments={**BAZI, "solar_time": "true_solar", "longitude": 116.4})
    assert result.status == "error" and "TAIBU_CONVENTION_UNRESOLVED" in result.content


@pytest.mark.asyncio
async def test_wrong_almanac_day_is_rejected(stack):
    """协议成功不能掩盖查询日期被上游默认或更换。"""
    result = await invoke(stack, name="taibu_almanac", arguments={"date": "2026-09-13"})
    assert result.status == "error" and "TAIBU_RESULT_INVALID" in result.content


@pytest.mark.asyncio
async def test_large_projection_keeps_raw_reference_and_bounded_error(stack):
    """大结果归档后仍有可回读引用，不通过增大全局预算掩盖问题。"""
    stack.remote.results["bazi"]["structuredContent"]["干支关系"] = ["测试关系" * 6000]
    result = await invoke(stack)
    assert (
        result.status == "error"
        and len(result.content.encode()) < stack.settings.taibu_projection_bytes
    )
    assert "TAIBU_PROJECTION_TOO_LARGE" in result.content
    assert len(stack.artifacts.read(result.artifact["artifact_id"], context=stack.context)) > 50_000


@pytest.mark.asyncio
async def test_json_escaping_cannot_overflow_outer_artifact_budget(stack):
    """外层消息再次编码会放大引号，必须连同引用一起控制真实字节数。"""
    stack.artifacts.inline_bytes = 4096
    stack.remote.results["bazi"]["structuredContent"]["干支关系"] = ['"' * 600]
    result = await invoke(stack)
    payload = json.dumps(
        {"content": result.content, "artifact": result.artifact}, ensure_ascii=False
    )
    assert len(payload.encode()) <= stack.artifacts.inline_bytes
    assert result.status == "error" and "TAIBU_PROJECTION_TOO_LARGE" in result.content


@pytest.mark.asyncio
async def test_missing_birth_minute_is_public_clarification_without_raw_input(stack):
    """框架校验错误只返回字段路径，不回显整份出生记录或可信 runtime。"""
    result = await invoke(
        stack, arguments={key: value for key, value in BAZI.items() if key != "birth_minute"}
    )
    assert result.status == "error" and "birth_minute" in result.content
    assert "1990" not in result.content and "tenant-taibu" not in result.content
    assert not stack.remote.calls
