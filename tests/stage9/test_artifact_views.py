"""大结果文件的业务视图、分页完整性和原文件引用回收。"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from financeclaw.agent_server.context.artifacts import ToolResultArchive
from financeclaw.agent_server.memory.history import HistoryService
from financeclaw.agent_server.middleware.artifact_middleware import ToolResultArtifactMiddleware
from financeclaw.agent_server.tools.history import history_tools
from financeclaw.shared.artifacts.views import (
    READ_BYTES,
    REFERENCE_BYTES,
    VIEW_KEY,
    archive_payload,
    at,
    business_view,
    encode,
    read_view,
    reference_view,
)
from tests.stage11.test_context_evidence import stack as evidence_stack

stack = evidence_stack


def test_archive_keeps_full_mcp_and_uses_one_business_source():
    """远端双通道都保留，但不再复制第三份模型正文。"""
    data = {"hotels": [{"name": "测试酒店", "price": 800}]}
    raw = {
        "response": {"structuredContent": data, "content": [{"type": "text", "text": encode(data)}]}
    }
    payload = archive_payload(encode(data), raw, name="mcp", status="success", mcp=True)
    assert payload["raw"]["artifact"] == raw
    assert "content" not in payload["raw"]
    assert business_view(payload) == ("json", data)
    old = {"name": "mcp", "status": "success", "artifact": {"mcp": {}, **raw}, "content": "old"}
    assert business_view(old) == ("json", data)


@pytest.mark.parametrize(
    "text,kind,value",
    [
        ('{"a":null}', "json", {"a": None}),
        ("普通文本", "text", "普通文本"),
    ],
)
def test_text_only_mcp_format(text, kind, value):
    """单文本块按真实格式识别；错误结果保持错误正文而非成功业务视图。"""
    raw = {"response": {"content": [{"type": "text", "text": text}]}}
    saved = archive_payload(text, raw, name="mcp", status="success", mcp=True)
    assert business_view(saved) == (kind, value)
    failed = archive_payload("MCP_OUTPUT_INVALID", raw, name="mcp", status="error", mcp=True)
    assert business_view(failed) == ("text", "MCP_OUTPUT_INVALID")


def test_multiple_text_blocks_and_binary_have_explicit_modes():
    """多段文本不被误合并成 JSON，图片也不暴露为文本数据。"""
    raw = {"response": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}
    saved = archive_payload("", raw, name="mcp", status="success", mcp=True)
    assert business_view(saved) == ("text", "a\nb")
    raw["response"]["content"] = [{"type": "image", "data": "opaque"}]
    kind, data = business_view(archive_payload("", raw, name="mcp", status="success", mcp=True))
    assert read_view(kind, data, {}, mode="inspect")["readable"] is False
    with pytest.raises(ValueError, match="no JSON or text"):
        read_view(kind, data, {}, mode="json")


def test_directory_and_preview_remain_bounded_and_report_coverage():
    """超长字段和预览只缩减目录，不修改原始结果或声称完整。"""
    data = {
        "rows": [{"name": "酒店" * 8000, "price": i} for i in range(123)],
        **{("字段" * 500 + str(i)): i for i in range(30)},
    }
    ref = reference_view({"artifact_id": "test", "content_hash": "a" * 64}, "json", data)
    assert len(encode(ref).encode()) < REFERENCE_BYTES
    assert ref["preview_complete"] is False
    assert ref["directory_complete"] is False
    assert len(data["rows"]) == 123
    ref = reference_view({}, "json", data, {"collection_path": "/missing"})
    assert ref["preview_unavailable"]


def test_record_pages_never_skip_and_keep_nested_values_and_null():
    """按最终字节分页，Unicode 和转义不会引起漏条或破碎 JSON。"""
    rows = [
        {"name": f"测试{i}", "room": {"price": i, "policy": '中\\"' * 100}, "none": None}
        for i in range(123)
    ]
    offset, seen = 0, []
    while True:
        page = read_view(
            "json",
            {"a/b": {"~rooms": rows}},
            {},
            mode="json",
            path="/a~1b/~0rooms",
            fields=["/name", "/room/price", "/none", "/absent"],
            start=offset,
            limit=200,
            max_bytes=1800,
        )
        assert len(encode(page).encode()) <= 1800
        for record in page["records"]:
            assert record["data"]["none"] is None
            assert record["missing_fields"] == ["/absent"]
            assert list(record["data"]["room"]) == ["price"]
            seen.append(record["index"])
        if page["next_start"] is None:
            break
        assert page["next_start"] > offset
        offset = page["next_start"]
    assert seen == list(range(123))
    assert len(rows[0]["room"]["policy"]) > 100


def test_inspected_nested_paths_can_be_used_for_the_next_read():
    """目录路径相对业务根，字段选择相对当前记录，包含 JSON Pointer 转义。"""
    data = {"outer": {"a/b": [{"price": 800}]}}
    info = read_view("json", data, {}, mode="inspect", path="/outer")
    path = info["collections"][0]["path"]
    assert path == "/outer/a~1b"
    assert info["fields"][0]["field"] == "/a~1b"
    page = read_view("json", data, {}, mode="json", path=path, fields=["/price"])
    assert page["records"][0]["data"] == {"price": 800}


def test_large_single_record_is_narrowed_not_skipped_or_split():
    """大记录返回目录，改选字段后仍能拿到原位置的数据。"""
    rows = [{"name": "a", "details": "很大" * 20000}, {"name": "b"}]
    page = read_view("json", rows, {}, mode="json")
    assert page["status"] == "needs_narrower_selection"
    assert page["returned_records"] == 0 and page["next_start"] is None
    page = read_view("json", rows, {}, mode="json", fields=["/name"])
    assert [r["data"]["name"] for r in page["records"]] == ["a", "b"]
    assert read_view("json", [], {}, mode="json")["next_start"] is None
    assert read_view("json", rows, {}, mode="json", start=20)["records"] == []
    with pytest.raises(ValueError, match="pointer"):
        at(rows, "/01")
    with pytest.raises(ValueError, match="pointer"):
        at({}, "/bad~2")


@pytest.mark.parametrize("start", [0, 10])
def test_object_fields_ignore_array_pagination_through_history_tool(stack, start):
    """复现酒店详情根对象携带 limit=1，走实际归档和读取工具仍成功。"""
    _, conversations, context, _, artifacts = stack
    data = {"name": "测试酒店", "checkIn": "2026-10-18", "starRating": 5, "extra": "details"}
    message = ToolMessage(
        content=encode(data),
        name="mcp",
        tool_call_id="hotel-object",
        artifact={"response": {"structuredContent": data}},
        additional_kwargs={VIEW_KEY: {"mcp": True}},
    )
    ref = ToolResultArchive(artifacts).project(message, context).artifact
    tool = history_tools(HistoryService(conversations, artifacts))[-1].tool
    response = tool._run(
        runtime=SimpleNamespace(context=context, tool_call_id="read-hotel"),
        artifact_id=ref["artifact_id"],
        content_hash=ref["content_hash"],
        mode="json",
        path="",
        fields=["/name", "/checkIn", "/starRating"],
        start=start,
        limit=1,
    )
    result = json.loads(response.content)
    assert result["data"] == {key: data[key] for key in ("name", "checkIn", "starRating")}
    assert "next_start" not in result
    assert response.additional_kwargs["artifact_ref"]["artifact_id"] == ref["artifact_id"]
    assert len(response.content.encode()) <= READ_BYTES


def test_text_cursor_uses_actual_characters_under_utf8_budget():
    """文本页按实际字符推进，而不是按请求的字符数跳页。"""
    text = '中文\\"' * 10000
    offset, pieces = 0, []
    while True:
        page = read_view("text", text, {}, mode="text", offset=offset, max_chars=8000)
        assert len(encode(page).encode()) <= READ_BYTES
        pieces.append(page["content"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page["content"])
        offset = page["next_offset"]
    assert "".join(pieces) == text


def test_empty_array_still_enforces_total_response_budget():
    """空页也计算字段选择元数据，过长请求返回可纠正错误而非超大正文。"""
    with pytest.raises(ValueError, match="select fewer or shorter fields"):
        read_view("json", [], {}, mode="json", fields=["/" + "字" * 900] * 24)


def test_projected_reference_counts_the_actual_serialized_body(stack):
    """完整 ToolMessage 使用与目录裁剪一致的序列化方式，外层标记也在上限内。"""
    _, _, context, _, artifacts = stack
    data = {"字段" * 40 + str(i): i for i in range(24)}
    message = ToolMessage(content=encode(data), name="large", tool_call_id="directory")
    projected = ToolResultArchive(artifacts).project(message, context)
    assert len(projected.content.encode()) <= REFERENCE_BYTES
    assert json.loads(projected.content)["historical_tool_result"]


def test_small_business_body_is_not_offloaded_due_to_large_attached_raw(stack):
    """独立正文阈值不受附带审计数据影响；外部工具不能伪造回读引用。"""
    _, _, context, _, artifacts = stack
    middleware = ToolResultArtifactMiddleware(artifacts)
    message = ToolMessage(
        content="small",
        artifact={"raw": "x" * 100000},
        name="external",
        tool_call_id="test",
        additional_kwargs={"artifact_ref": {"artifact_id": "fake"}},
    )
    request = SimpleNamespace(runtime=Runtime(context=context), tool_call={"name": "external"})
    result = middleware._project(request, message)
    assert result.content == "small" and "artifact_ref" not in result.additional_kwargs


def test_reader_cleanup_reuses_original_file_and_its_query(stack):
    """读取回执和压缩都指向同一原始文件，不创建页面归档链。"""
    _, conversations, context, _, artifacts = stack
    content = {"rows": [{"name": f"酒店{i}", "price": 800} for i in range(123)]}
    message = ToolMessage(
        content=encode(content),
        name="mcp",
        tool_call_id="original",
        artifact={"response": {"structuredContent": content}},
        additional_kwargs={VIEW_KEY: {"mcp": True}},
    )
    archive = ToolResultArchive(artifacts)
    ref = archive.project(message, context).artifact
    tool = history_tools(HistoryService(conversations, artifacts))[-1].tool
    runtime = SimpleNamespace(context=context, tool_call_id="read-1")
    page = tool._run(
        runtime=runtime,
        artifact_id=ref["artifact_id"],
        content_hash=ref["content_hash"],
        mode="json",
        path="/rows",
        fields=["/name"],
        limit=200,
    )
    assert json.loads(page.content)["returned_records"] == 123
    middleware = ToolResultArtifactMiddleware(artifacts, reader_tools=frozenset({"read_artifact"}))
    page = middleware._project(
        SimpleNamespace(runtime=runtime, tool_call={"name": "read_artifact"}), page
    )
    projected = archive.project(page, context)
    assert projected.artifact["artifact_id"] == ref["artifact_id"]
    assert projected.artifact["last_read"]["path"] == "/rows"
    assert (
        len(
            artifacts.repository.list_turn(
                context.conversation_id, context.turn_id, context.tenant_id, context.subject_id
            )
        )
        == 1
    )
