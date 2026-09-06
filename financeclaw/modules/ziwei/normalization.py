"""确定性出生时间和查询区间规范化，不由模型手算日历或时区。"""

import hashlib
import hmac
import json
from datetime import UTC, date, datetime, time, timedelta

from financeclaw.kernel import ExecutionContext

from .errors import ZiweiError
from .models import (
    BirthContext,
    ChartLevel,
    ResolvedTarget,
    ZiweiAnalysisRequest,
    ZiweiConvention,
)
from .ports import ZiweiEngine

SHICHEN = (
    "zi_early",
    "chou",
    "yin",
    "mao",
    "chen",
    "si",
    "wu",
    "wei",
    "shen",
    "you",
    "xu",
    "hai",
    "zi_late",
)
# 一期仅解析这一组无网络的明确城市别名，其他地名需要用户提供 IANA 时区。
CITY_ZONES = {
    "北京": ("CN", "Asia/Shanghai"),
    "上海": ("CN", "Asia/Shanghai"),
    "广州": ("CN", "Asia/Shanghai"),
    "深圳": ("CN", "Asia/Shanghai"),
    "成都": ("CN", "Asia/Shanghai"),
    "香港": ("HK", "Asia/Hong_Kong"),
    "台北": ("TW", "Asia/Taipei"),
}


def canonical(value: object) -> str:
    """确定性紧凑 JSON；签名和事实比较不依赖字典插入顺序。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def birth_context(
    request: ZiweiAnalysisRequest,
    context: ExecutionContext,
    engine: ZiweiEngine,
    convention: ZiweiConvention,
    *,
    hmac_key: bytes,
    key_version: str,
) -> BirthContext:
    """校验缺失、时区和精度，并给规范化资料生成租户／主体隔离的指纹。"""
    birth = request.birth
    missing = tuple(
        f"birth.{name}"
        for name in ("calendar", "date", "time_basis", "place", "sex_for_chart")
        if getattr(birth, name) is None
    )
    if birth.time.kind == "unknown":
        missing += ("birth.time",)
    if birth.calendar == "lunar" and birth.is_leap_month is None:
        missing += ("birth.is_leap_month",)
    if missing:
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请补充：" + "、".join(missing), missing)
    if birth.time_basis != convention.time_basis:
        raise ZiweiError(
            "ZIWEI_CONVENTION_UNSUPPORTED", "当前候选仅验证民用钟表时间，尚未支持真太阳时。"
        )
    assert birth.place is not None and birth.sex_for_chart is not None
    known = CITY_ZONES.get(birth.place.name.removesuffix("市"))
    zone_name = birth.place.timezone
    if known:
        if (birth.place.country_code and birth.place.country_code != known[0]) or (
            zone_name and zone_name != known[1]
        ):
            raise ZiweiError(
                "ZIWEI_PLACE_AMBIGUOUS", "地点与时区／地区不一致，请确认。", ("birth.place",)
            )
        zone_name = known[1]
    if zone_name is None:
        raise ZiweiError(
            "ZIWEI_PLACE_AMBIGUOUS",
            "请补充出生地的 IANA 时区，当前不使用在线地理编码。",
            ("birth.place.timezone",),
        )
    zone = engine.zone(zone_name)
    solar = engine.solar_date(birth)
    if not 1901 <= solar.year <= 2099:
        raise ZiweiError("ZIWEI_DATE_UNSUPPORTED", "当前候选日期范围为 1901–2099。")
    warnings = ["候选排盘规则尚待独立核验；结果仅供传统文化参考。"]
    if solar.year < 1970:
        warnings.append("早期历史时区资料可能不完整，请核对出生记录所用时间。")
    if not known:
        warnings.append("出生地时区采用用户明确提供的值，未进行地理编码核验。")
    supplied = birth.time
    instant = None
    if supplied.kind == "shichen":
        slot = supplied.shichen
        if slot is None or slot == "zi":
            raise ZiweiError(
                "ZIWEI_TIME_AMBIGUOUS",
                "请补充具体时辰；子时需区分当日早子时或晚子时。",
                ("birth.time",),
            )
        warnings.append("出生记录仅精确到时辰，不生成精确 UTC 出生时刻。")
    else:
        if supplied.clock is None or (supplied.kind == "range" and supplied.end is None):
            raise ZiweiError(
                "ZIWEI_INPUT_INCOMPLETE", "请补充钟表时间或完整时间区间。", ("birth.time",)
            )
        start = datetime.combine(solar, time.fromisoformat(supplied.clock))
        end = datetime.combine(solar, time.fromisoformat(supplied.end or supplied.clock))
        if end < start:
            raise ZiweiError(
                "ZIWEI_TIME_AMBIGUOUS", "跨日出生区间需先确认具体日期。", ("birth.time",)
            )
        slots = set()
        cursor = start
        while cursor <= end:
            possibilities = {
                dt.astimezone(UTC)
                for fold in (0, 1)
                for dt in (cursor.replace(tzinfo=zone, fold=fold),)
                if dt.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == cursor
                and (supplied.fold is None or supplied.fold == fold)
            }
            if len(possibilities) != 1:
                raise ZiweiError(
                    "ZIWEI_TIME_AMBIGUOUS",
                    "该钟表时间不存在或因夏令时重复，请核对时间及 fold。",
                    ("birth.time",),
                )
            slots.add(SHICHEN[(cursor.hour + 1) // 2])
            if supplied.kind == "clock":
                instant = next(iter(possibilities))
            cursor += timedelta(minutes=1)
        if len(slots) != 1:
            raise ZiweiError(
                "ZIWEI_TIME_AMBIGUOUS", "出生时间区间跨时辰，请进一步确认。", ("birth.time",)
            )
        slot = slots.pop()
        if supplied.kind == "range":
            warnings.append("出生区间落在同一排盘时辰；保留区间精度，不虚构精确时刻。")
    identity = {
        "tenant": context.tenant_id,
        "subject": context.subject_id,
        "solar_date": solar.isoformat(),
        "shichen": slot,
        "sex": birth.sex_for_chart,
        "timezone": zone_name,
        "time": supplied.model_dump(mode="json"),
        "place": birth.place.model_dump(mode="json"),
        "convention": convention.model_dump(mode="json"),
        "key_version": key_version,
    }
    fingerprint = hmac.new(hmac_key, canonical(identity).encode(), hashlib.sha256).hexdigest()
    return BirthContext(
        solar_date=solar,
        shichen=slot,
        sex_for_chart=birth.sex_for_chart,
        timezone=zone_name,
        utc_instant=instant,
        time_precision=supplied.kind,
        fingerprint=fingerprint,
        owner_fingerprint=hmac.new(
            hmac_key, canonical([context.tenant_id, context.subject_id]).encode(), hashlib.sha256
        ).hexdigest(),
        key_version=key_version,
        convention_ref=convention.ref,
        warnings=tuple(warnings),
    )


def resolve_target(
    request: ZiweiAnalysisRequest, context: ExecutionContext, engine: ZiweiEngine
) -> ResolvedTarget | None:
    """按固定 Turn 时间解析民用日期区间，不用日期代表点偷换整年或整月。"""
    selector = request.target
    if request.level is ChartLevel.NATAL:
        if selector is not None:
            raise ZiweiError(
                "ZIWEI_INPUT_INCOMPLETE", "本命盘无需目标日期，请确认要查询的流运层级。", ("level",)
            )
        return None
    if selector is None:
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请指定要查询的日期或区间。", ("target",))
    if not context.request_clock:
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "运行缺少可信请求时间，请重新发起查询。")
    try:
        clock = datetime.fromisoformat(context.request_clock)
        if clock.tzinfo is None:
            raise ValueError("naive clock")
        today = clock.astimezone(engine.zone(context.timezone)).date()
    except ValueError:
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "运行的请求时间或时区无效。") from None
    if selector.calendar == "lunar":
        raise ZiweiError("ZIWEI_DATE_UNSUPPORTED", "当前查询区间仅支持公历；出生资料可使用农历。")
    try:
        if selector.kind == "point":
            if selector.on_date is None:
                raise ValueError("date missing")
            start, end = selector.on_date, selector.on_date + timedelta(days=1)
        elif selector.kind == "bounded_range":
            if selector.start is None or selector.end is None:
                raise ValueError("range missing")
            start, end = selector.start, selector.end
        else:
            unit = selector.unit
            year, month = selector.year, selector.month
            if selector.kind == "relative_period":
                year, month = today.year, today.month
                if unit == "year":
                    year += selector.offset
                elif unit == "month":
                    offset = year * 12 + month - 1 + selector.offset
                    year, month = offset // 12, offset % 12 + 1
                elif unit == "day":
                    today += timedelta(days=selector.offset)
            if unit == "year" and year is not None:
                if selector.month is not None:
                    raise ValueError("year cannot contain month")
                start, end = date(year, 1, 1), date(year + 1, 1, 1)
            elif unit == "month" and year is not None and month is not None:
                start = date(year, month, 1)
                end = date(year + (month == 12), month % 12 + 1, 1)
            elif unit == "day" and selector.kind == "relative_period":
                start, end = today, today + timedelta(days=1)
            else:
                raise ValueError("period fields missing")
        if start.year < 1901 or (end - timedelta(days=1)).year > 2099:
            raise ValueError("date range unsupported")
        return ResolvedTarget(start=start, end=end, timezone=context.timezone, request_clock=clock)
    except ValueError:
        raise ZiweiError(
            "ZIWEI_RANGE_LIMIT", "请提供有效且不超过 366 天的公历查询区间。", ("target",)
        ) from None
