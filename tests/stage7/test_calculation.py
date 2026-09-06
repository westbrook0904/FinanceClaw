"""真实引擎与确定性规范化测试；回归断言不等同于独立排盘权威认证。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from financeclaw.modules.ziwei.errors import ZiweiError
from financeclaw.modules.ziwei.models import ChartLevel, ZiweiAnalysisRequest
from tests.stage7.support import components, context, request


def test_all_five_levels_have_consistent_natal_and_atomic_ancestors():
    """五个入口共有的本命完全一致，日盘直接具有全部流运层级。"""
    service = components().ziwei_service
    results = []
    for level in ChartLevel:
        query = (
            request(level=level, target=None) if level == ChartLevel.NATAL else request(level=level)
        )
        birth, target = service.preflight(query, context())
        result = service.calculate(birth, target, level, "overall", context())
        results.append(result)
        assert len(result.model_dump_json().encode()) < 14_000
        assert {f.layer for f in result.facts} == set(
            tuple(ChartLevel)[: tuple(ChartLevel).index(level) + 1]
        )
    natal = tuple(f for f in results[0].facts if f.layer == "natal")
    assert all(tuple(f for f in result.facts if f.layer == "natal") == natal for result in results)
    # 上游示例的可检查盘面字段；并非独立算法真值。
    meta = json.loads(natal[0].value_json)
    assert meta["fiveElementsClass"] == "木三局"
    assert meta["earthlyBranchOfSoulPalace"] == "午"


def test_solar_and_lunar_equivalence_and_invalid_leap_flag():
    """农历 round-trip 拒绝引擎原本会忽略的无效闰月参数。"""
    service = components().ziwei_service
    solar = request()
    lunar = solar.model_dump(mode="json")
    lunar["birth"].update(
        calendar="lunar", date={"year": 2000, "month": 7, "day": 17}, is_leap_month=False
    )
    a, _ = service.preflight(solar, context())
    b, _ = service.preflight(ZiweiAnalysisRequest.model_validate(lunar), context())
    assert a.fingerprint == b.fingerprint and a.solar_date == b.solar_date
    lunar["birth"]["is_leap_month"] = True
    with pytest.raises(ZiweiError, match="闰月"):
        service.preflight(ZiweiAnalysisRequest.model_validate(lunar), context())


@pytest.mark.parametrize(
    "clock,slot",
    [
        ("00:00", "zi_early"),
        ("00:59", "zi_early"),
        ("01:00", "chou"),
        ("22:59", "hai"),
        ("23:00", "zi_late"),
    ],
)
def test_shichen_boundaries_and_single_late_zi_adjustment(clock, slot):
    """不提前加日；由固定引擎配置执行晚子时规则。"""
    service = components().ziwei_service
    data = request().model_dump(mode="json")
    data["birth"]["time"]["clock"] = clock
    birth, _ = service.preflight(ZiweiAnalysisRequest.model_validate(data), context())
    assert birth.shichen == slot and birth.solar_date.isoformat() == "2000-08-16"


@pytest.mark.parametrize("day,clock", [(10, "02:30"), (3, "01:30")])
def test_dst_gap_and_fold_require_clarification(day, clock):
    """纽约 2024 春季缺口与秋季重复时刻均不能静默解释。"""
    service = components().ziwei_service
    data = request().model_dump(mode="json")
    data["birth"].update(
        date={"year": 2024, "month": 3 if day == 10 else 11, "day": day},
        place={"name": "New York", "timezone": "America/New_York"},
    )
    data["birth"]["time"]["clock"] = clock
    with pytest.raises(ZiweiError) as error:
        service.preflight(ZiweiAnalysisRequest.model_validate(data), context())
    assert error.value.code == "ZIWEI_TIME_AMBIGUOUS"


def test_unknown_and_uncertain_time_are_not_fabricated():
    """同一时辰区间可以继续，跨时辰和未区分早晚的子时必须澄清。"""
    service = components().ziwei_service
    data = request().model_dump(mode="json")
    data["birth"]["time"] = {"kind": "range", "clock": "03:10", "end": "04:50"}
    birth, _ = service.preflight(ZiweiAnalysisRequest.model_validate(data), context())
    assert birth.utc_instant is None and birth.shichen == "yin"
    for value in (
        {"kind": "unknown"},
        {"kind": "shichen", "shichen": "zi"},
        {"kind": "range", "clock": "04:50", "end": "05:10"},
    ):
        data["birth"]["time"] = value
        with pytest.raises(ZiweiError) as error:
            service.preflight(ZiweiAnalysisRequest.model_validate(data), context())
        assert error.value.fields


def test_year_range_is_split_at_real_lunar_year_boundary():
    """公历 2026 整年不能用 1 月 1 日代表；覆盖连续且有两个流年片段。"""
    service = components().ziwei_service
    query = request(
        level="yearly", target={"kind": "calendar_period", "unit": "year", "year": 2026}
    )
    birth, target = service.preflight(query, context())
    result = service.calculation.calculate_snapshot(birth, target, query.level)
    assert result.segments[0].start.isoformat() == "2026-01-01"
    assert result.segments[-1].end.isoformat() == "2027-01-01"
    assert len(result.segments) == 2
    assert result.segments[0].end == result.segments[1].start
    assert result.segments[1].start.isoformat() == "2026-02-17"


def test_relative_target_uses_request_clock_and_query_timezone():
    """不依赖测试执行机器的当前日期，也不使用出生时区解析今天。"""
    service = components().ziwei_service
    query = request(target={"kind": "relative_period", "unit": "day"})
    _, target = service.preflight(query, context(timezone="America/New_York"))
    assert target.start.isoformat() == "2026-09-05"
    with pytest.raises(ZiweiError):
        service.preflight(query, context(request_clock=None))


def test_no_cross_owner_reuse_and_concurrent_determinism():
    """共享 Tool/Service 不持有当前命盘；跨主体读取冻结出生上下文失败。"""
    service = components().ziwei_service
    query = request()
    birth, target = service.preflight(query, context())
    with pytest.raises(PermissionError):
        service.calculate(birth, target, query.level, query.focus, context(subject_id="other"))

    def calculate(_):
        """多个工作线程复用服务配置但不复用可变引擎对象。"""
        return service.calculate(birth, target, query.level, query.focus, context())

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(calculate, range(12)))
    assert all(result == results[0] for result in results)


def test_artifact_is_idempotent_across_request_clocks_and_protected(tmp_path):
    """内容寻址不包含计算时间；读取仍检查 artifacts:read 与 owner。"""
    stack = components(tmp_path)
    service = stack.ziwei_service
    try:
        first_birth, first_target = service.preflight(request(), context())
        first = service.calculate(first_birth, first_target, ChartLevel.DAILY, "overall", context())
        later = context(request_clock="2026-09-07T01:00:00+08:00")
        birth, target = service.preflight(request(), later)
        second = service.calculate(birth, target, ChartLevel.DAILY, "overall", later)
        assert first.artifact == second.artifact
        metadata = stack.artifact_service.repository.get_owned(
            first.artifact.artifact_id, context().tenant_id, context().subject_id
        )
        assert metadata.access_policy["data_classification"] == "confidential"
    finally:
        stack.database.close()


def test_over_budget_and_unsupported_rules_fail_without_partial_analysis():
    """不会因预算或太阳时不支持而换口径、裁 JSON 或编造结论。"""
    service = components().ziwei_service
    data = request().model_dump(mode="json")
    data["birth"]["time_basis"] = "apparent_solar"
    with pytest.raises(ZiweiError) as error:
        service.preflight(ZiweiAnalysisRequest.model_validate(data), context())
    assert error.value.code == "ZIWEI_CONVENTION_UNSUPPORTED"
    birth, target = service.preflight(request(), context())
    service.projection_bytes = 1024
    with pytest.raises(ZiweiError) as error:
        service.calculate(birth, target, ChartLevel.DAILY, "overall", context())
    assert error.value.code == "ZIWEI_CONTEXT_BUDGET_EXCEEDED"
