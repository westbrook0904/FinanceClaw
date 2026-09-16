"""按已观察的数据规模回放酒店推荐链路；合成回包，不代表真实模型验收。"""

import json
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mcp.types import CallToolResult

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.tools.mcp_transport import MCPTransport
from financeclaw.shared.artifacts.views import READ_BYTES, REFERENCE_BYTES, encode
from tests.mcp_integration.test_execution import seed_root


class HotelReaderModel(OfflineFinanceModel):
    """只负责确定性的调用选择，归档、JSON 读取和根图均运行真实代码。"""

    observed: ClassVar[list] = []

    def _generate(self, messages, *args, **kwargs):
        """读取实际回执的引用和游标，不在测试模型中猜文件 ID 或下一页。"""
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        type(self).observed.append(len(receipts))
        by_id = {m.tool_call_id: json.loads(m.content) for m in receipts}
        calls = []
        if "search" not in by_id:
            calls = [("search", "mcp__hotel__search", {"query": "合成测试城市"})]
        elif "read-search" not in by_id:
            calls = [
                (
                    "read-search",
                    "read_artifact",
                    self._read_args(
                        by_id["search"], "/hotelInformationList", ["/hotelId", "/name", "/price"], 0
                    ),
                )
            ]
        else:
            for record in by_id["read-search"]["records"][:3]:
                hotel_id = record["data"]["hotelId"]
                key = f"detail-{hotel_id}"
                if key not in by_id:
                    calls.append((key, "mcp__hotel__detail", {"hotel_id": hotel_id}))
            if not calls:
                for record in by_id["read-search"]["records"][:3]:
                    hotel_id = record["data"]["hotelId"]
                    ref = by_id[f"detail-{hotel_id}"]
                    pages = [v for k, v in by_id.items() if k.startswith(f"page-{hotel_id}-")]
                    next_start = pages[-1]["next_start"] if pages else 0
                    if next_start is not None:
                        calls.append(
                            (
                                f"page-{hotel_id}-{next_start}",
                                "read_artifact",
                                self._read_args(
                                    ref,
                                    "/roomRatePlans",
                                    [
                                        "/roomName",
                                        "/averagePrice",
                                        "/currency",
                                        "/mealTypeStr",
                                        "/cancelable",
                                    ],
                                    next_start,
                                ),
                            )
                        )
        if calls:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": key,
                        "name": name,
                        "args": arguments,
                        "type": "tool_call",
                    }
                    for key, name, arguments in calls
                ],
            )
        else:
            count = sum(v["returned_records"] for k, v in by_id.items() if k.startswith("page-"))
            answer = AIMessage(content=f"已读取三家酒店，共 {count} 条房价方案。")
        return ChatResult(generations=[ChatGeneration(message=answer)])

    @staticmethod
    def _read_args(ref, path, fields, start):
        """使用实际回执绑定来源，字段相对于业务数据根。"""
        return {
            "artifact_id": ref["artifact_id"],
            "content_hash": ref["content_hash"],
            "mode": "json",
            "path": path,
            "fields": fields,
            "start": start,
            "limit": 200,
        }


@pytest.mark.asyncio
async def test_large_hotel_results_complete_within_existing_budget(
    settings, context, config_path, monkeypatch, record_property
):
    """10 家候选、35/100/123 条房价方案在原 8/12 限额内完成结构化回读。"""
    config_path.write_text(
        config_path.read_text()
        + """
[servers.hotel.result_views.search]
delivery = "reference"
collection_path = "/hotelInformationList"
preview_fields = ["/hotelId", "/name", "/price"]
[servers.hotel.result_views.detail]
delivery = "reference"
collection_path = "/roomRatePlans"
preview_fields = ["/roomName", "/averagePrice", "/currency"]
"""
    )
    business_calls = []

    async def saved_response(transport, entry, arguments):
        """只替换远端返回；保留实际 MCP 工具装配、回包和归档中间件。"""
        business_calls.append((entry.definition.name, arguments))
        if entry.definition.name == "search":
            data = {
                "hotel_id": "hotel-0",
                "hotelInformationList": [
                    {
                        "hotelId": f"hotel-{i}",
                        "name": f"合成酒店{i}",
                        "price": 800 + i,
                        "description": "酒店设施介绍" * 1000,
                    }
                    for i in range(10)
                ],
            }
        else:
            count = {"hotel-0": 35, "hotel-1": 100, "hotel-2": 123}[arguments["hotel_id"]]
            data = {
                "roomRatePlans": [
                    {
                        "roomName": f"测试房型{i}",
                        "averagePrice": 700 + i,
                        "currency": "CNY",
                        "mealTypeStr": "含早餐",
                        "cancelable": True,
                        "cancelPolicy": "取消条件" * 150,
                        "roomInfo": {"description": "房间介绍" * 100},
                    }
                    for i in range(count)
                ]
            }
        return CallToolResult(
            content=[{"type": "text", "text": encode(data)}], structuredContent=data
        )

    monkeypatch.setattr(MCPTransport, "call", saved_response)
    components = build_components(settings, enable_persistence=True)
    try:
        context = seed_root(components, context, "hotel-views")
        HotelReaderModel.observed = []
        graph = components.agent_factory.build(
            components.default_agent_profile, model=HotelReaderModel()
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="比较三家酒店", id="mcp-input")]},
            config={"configurable": {"thread_id": "hotel-views"}},
            context=context,
        )
        assert "258 条房价方案" in result["messages"][-1].content
        assert len(business_calls) == 4
        assert len(HotelReaderModel.observed) <= 8
        receipts = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(receipts) <= 12 and all(m.status == "success" for m in receipts)
        for receipt in receipts:
            cap = READ_BYTES if receipt.name == "read_artifact" else REFERENCE_BYTES
            assert len(receipt.content.encode()) <= cap
        files = components.artifact_service.repository.list_turn(
            context.conversation_id,
            context.turn_id,
            context.tenant_id,
            context.subject_id,
        )
        assert len(files) == 4
        turn = components.conversation_repository.execution.get(context.turn_id)
        assert turn["tool_calls"] == len(receipts)
        assert turn["model_calls"] == len(HotelReaderModel.observed)
        for key, value in {
            "business_calls": len(business_calls),
            "tool_calls": len(receipts),
            "model_calls": len(HotelReaderModel.observed),
            "archive_count": len(files),
            "max_reply_bytes": max(len(m.content.encode()) for m in receipts),
            "rate_plan_records": 258,
        }.items():
            record_property(key, value)
    finally:
        components.database.close()
