"""Tool 内的参数错误聚合；不调用模型，也不创建单独的图阶段。"""

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.kernel.ziwei import ZiweiInputIssue

LABELS = {
    "birth.calendar": "出生日期使用公历还是农历",
    "birth.date": "出生日期（年、月、日）",
    "birth.date.year": "出生年份",
    "birth.date.month": "出生月份",
    "birth.date.day": "出生日期中的日",
    "birth.time": "出生时间或明确的时辰",
    "birth.time.clock": "出生钟表时间",
    "birth.time.end": "出生时间区间的结束时间",
    "birth.time_basis": "出生记录所用时制（当地钟表时间或真太阳时）",
    "birth.place": "出生地点",
    "birth.place.name": "出生地点名称",
    "birth.place.timezone": "出生地时区",
    "birth.sex_for_chart": "排盘所用性别",
    "birth.is_leap_month": "该农历月份是否为闰月",
    "target": "要查询的日期或区间",
    "target.year": "要查询的年份",
    "target.month": "要查询的月份",
    "target.on_date": "要查询的具体日期",
    "target.start": "查询区间的开始日期",
    "target.end": "查询区间的结束日期",
    "target.unit": "查询年份、月份还是某一天",
}


def missing_error(fields: tuple[str, ...]) -> ZiweiError:
    """用一次问题列出缺失字段，不要求用户理解内部路径。"""
    fields = tuple(dict.fromkeys(fields))
    issues = tuple(
        ZiweiInputIssue(field=field, code="missing", message=LABELS.get(field, field))
        for field in fields
    )
    return ZiweiError(
        "ZIWEI_INPUT_INCOMPLETE",
        "请补充以下资料：\n" + "\n".join(f"- {issue.message}" for issue in issues),
        fields,
        issues=issues,
    )


def schema_error(arguments, error) -> ZiweiError:
    """聚合 Schema 问题及已可确定的缺失资料，不因嵌套字段缺失而漏问其他资料。"""
    from financeclaw.agent_server.domains.ziwei.normalization import CITY_ZONES

    issues = tuple(
        ZiweiInputIssue(
            field=".".join(map(str, item["loc"])) or "arguments",
            code=item["type"],
            message="参数格式不符合工具 Schema，请依据已有上下文修正，不能猜测缺失资料。",
        )
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )
    fields = [issue.field for issue in issues if issue.code == "missing"]
    birth = arguments.get("birth") or {}
    if isinstance(birth, dict):
        fields.extend(
            f"birth.{key}"
            for key in ("calendar", "date", "place", "sex_for_chart", "time_basis")
            if birth.get(key) is None
        )
        time = birth.get("time") or {}
        if isinstance(time, dict):
            if time.get("kind", "unknown") == "unknown":
                fields.append("birth.time")
            elif time.get("kind") in {"clock", "range"}:
                fields.extend(
                    f"birth.time.{key}"
                    for key in (("clock", "end") if time["kind"] == "range" else ("clock",))
                    if time.get(key) is None
                )
        if birth.get("calendar") == "lunar" and birth.get("is_leap_month") is None:
            fields.append("birth.is_leap_month")
        place = birth.get("place")
        if isinstance(place, dict) and isinstance(place.get("name"), str):
            if place["name"].removesuffix("市") not in CITY_ZONES and not place.get("timezone"):
                fields.append("birth.place.timezone")
    fields.extend(missing_target_fields(arguments.get("level", "natal"), arguments.get("target")))
    if fields:
        missing = missing_error(tuple(fields))
        invalid = tuple(issue for issue in issues if issue.code != "missing")
        return ZiweiError(
            missing.code,
            str(missing)
            + (
                "\n还需确认以下参数的格式："
                + "、".join(LABELS.get(issue.field, issue.field) for issue in invalid)
                if invalid
                else ""
            ),
            missing.fields,
            issues=missing.issues + invalid,
        )
    return ZiweiError(
        "ZIWEI_TOOL_INPUT_INVALID", "排盘参数格式有误，请按已提供的原文修正。", issues=issues
    )


def missing_target_fields(level, target) -> tuple[str, ...]:
    """同时列出已知查询类型所缺的全部目标字段。"""
    if level == "natal":
        return ()
    if target is None:
        return ("target",)
    if not isinstance(target, dict):
        return ()
    required = {
        "point": ("on_date",),
        "bounded_range": ("start", "end"),
        "calendar_period": ("unit", "year"),
        "relative_period": ("unit",),
    }.get(target.get("kind"), ())
    if target.get("kind") == "calendar_period" and target.get("unit") == "month":
        required += ("month",)
    return tuple(f"target.{key}" for key in required if target.get(key) is None)
