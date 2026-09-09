"""五个排盘入口的独立契约、时间转换与真实计算绑定。"""

from copy import deepcopy
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain.tools import ToolRuntime
from pydantic import ValidationError

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.domains.ziwei.normalization import resolve_target
from financeclaw.agent_server.domains.ziwei.tool_inputs import (
    analysis_request,
    public_input_error,
)
from financeclaw.agent_server.tools.ziwei import ziwei_tools
from financeclaw.kernel.ziwei import ZiweiAnalysisRequest
from financeclaw.kernel.ziwei_tools import ZIWEI_TOOL_INPUTS, ZIWEI_TOOL_LEVELS
from tests.stage7.support import components, context, request


@pytest.mark.parametrize("name", ZIWEI_TOOL_INPUTS)
@pytest.mark.parametrize("extra", [{"level": "daily"}, {"target": None}, {"runtime": {}}])
def test_fixed_level_and_runtime_cannot_be_supplied_by_the_model(name, extra):
    """不再接受旧选择器、层级覆盖或运行身份；这些不是可被静默忽略的参数。"""
    with pytest.raises(ValidationError) as error:
        analysis_request(name, extra)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    "name,arguments,missing",
    [
        ("ziwei_decadal_chart", {}, {"on_date"}),
        ("ziwei_yearly_chart", {}, {"year"}),
        ("ziwei_monthly_chart", {}, {"year", "month"}),
        ("ziwei_monthly_chart", {"month": 9}, {"year"}),
        ("ziwei_daily_chart", {}, {"on_date"}),
        ("ziwei_daily_chart", {"date_range": {"start": "2026-09-09"}}, {"date_range.end"}),
    ],
)
def test_missing_dates_are_not_defaulted_and_use_public_field_names(name, arguments, missing):
    """未提供目标时不能猜今天或今年，补充字段必须存在于所选工具的 Schema。"""
    parsed = analysis_request(name, arguments)
    with pytest.raises(ZiweiError) as error:
        resolve_target(parsed, context(), SimpleNamespace(zone=ZoneInfo))
    public = public_input_error(error.value)
    assert set(public.fields) == missing
    assert {issue.field for issue in public.issues} == missing


@pytest.mark.parametrize(
    "name,arguments,start,end",
    [
        ("ziwei_decadal_chart", {"day_offset": 0}, "2026-12-31", "2027-01-01"),
        ("ziwei_yearly_chart", {"year": 2026}, "2026-01-01", "2027-01-01"),
        ("ziwei_yearly_chart", {"year_offset": 1}, "2027-01-01", "2028-01-01"),
        ("ziwei_monthly_chart", {"year": 2026, "month": 9}, "2026-09-01", "2026-10-01"),
        ("ziwei_monthly_chart", {"month_offset": 1}, "2027-01-01", "2027-02-01"),
        ("ziwei_daily_chart", {"day_offset": 1}, "2027-01-01", "2027-01-02"),
        ("ziwei_daily_chart", {"day_offset": 0}, "2026-12-31", "2027-01-01"),
        (
            "ziwei_daily_chart",
            {"date_range": {"start": "2026-09-09", "end": "2026-09-12"}},
            "2026-09-09",
            "2026-09-12",
        ),
    ],
)
def test_each_tool_resolves_only_its_period_with_the_frozen_clock(name, arguments, start, end):
    """固定时钟的纽约日期仍在 2026 年末，包含跨年相对时间和完整期间。"""
    before = deepcopy(arguments)
    parsed = analysis_request(name, arguments)
    target = resolve_target(
        parsed,
        context(request_clock="2027-01-01T01:00:00+08:00", timezone="America/New_York"),
        SimpleNamespace(zone=ZoneInfo),
    )
    assert parsed.level == ZIWEI_TOOL_LEVELS[name]
    assert (target.start.isoformat(), target.end.isoformat()) == (start, end)
    assert arguments == before


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("ziwei_natal_chart", {"on_date": "2026-09-09"}),
        ("ziwei_decadal_chart", {"year": 2026}),
        ("ziwei_yearly_chart", {"month": 9}),
        ("ziwei_yearly_chart", {"year": 2026, "year_offset": 0}),
        ("ziwei_monthly_chart", {"month": 9, "month_offset": 0}),
        ("ziwei_daily_chart", {"year_offset": 1}),
        ("ziwei_daily_chart", {"on_date": "2026-09-09", "day_offset": 0}),
        (
            "ziwei_daily_chart",
            {"day_offset": 0, "date_range": {"start": "2026-09-09", "end": "2026-09-10"}},
        ),
    ],
)
def test_cross_level_and_conflicting_date_fields_are_rejected(name, arguments):
    """无关日期字段和相互冲突的时间表示不能被悄悄丢弃。"""
    with pytest.raises(ValidationError):
        analysis_request(name, arguments)


@pytest.mark.parametrize("target", [None, {"kind": "point", "on_date": "2026-09-09"}])
def test_natal_resolution_never_assigns_to_the_frozen_request(target):
    """兼容内部本命请求：直接返回无目标，不修改冻结实例，也不误报日期缺失。"""
    value = ZiweiAnalysisRequest(level="natal", target=target)
    before = value.model_dump()
    assert resolve_target(value, context(), None) is None
    assert value.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5))
async def test_each_registered_tool_runs_real_engine_and_binds_its_fixed_level(index):
    """走实际 BaseTool 的解析、同步／异步执行及证据输出，而非只测配置。"""
    stack = components()
    managed = ziwei_tools(stack.ziwei_service)[index]
    tool = managed.tool
    values = request().model_dump(mode="json", exclude={"level", "target"})
    if index:
        values["on_date"] = "2026-09-09"
    before = deepcopy(values)
    runtime = ToolRuntime(
        state={},
        context=context(),
        config={},
        stream_writer=lambda _: None,
        tool_call_id="fixed-chart",
        store=None,
    )
    call = {
        "name": tool.name,
        "id": "fixed-chart",
        "type": "tool_call",
        "args": {**values, "runtime": runtime},
    }
    result = await tool.ainvoke(call) if index % 2 else tool.invoke(call)
    record = result.update["ziwei_evidence"][0]
    assert record["tool_call_id"] == "fixed-chart"
    assert record["request"]["level"] == ZIWEI_TOOL_LEVELS[tool.name]
    assert record["projection"]["level"] == record["request"]["level"]
    assert record["projection"]["birth_fingerprint"] == record["birth_fingerprint"]
    assert record["target"] == record["projection"]["target"]
    assert result.update["messages"][0].name == tool.name == managed.governance.tool_id
    assert values == before
    denied = runtime.__class__(
        state={},
        context=context(scopes=set()),
        config={},
        stream_writer=lambda _: None,
        tool_call_id="denied",
        store=None,
    )
    with pytest.raises(PermissionError):
        tool.invoke({**call, "args": {**values, "runtime": denied}})
