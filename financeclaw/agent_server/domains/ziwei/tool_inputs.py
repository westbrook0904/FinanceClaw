"""将各层级工具的简单参数转换为统一计算契约，并保留公开错误字段路径。"""

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.domains.ziwei.validation import schema_error
from financeclaw.kernel.ziwei import ChartLevel, ZiweiAnalysisRequest, ZiweiChartParameters
from financeclaw.kernel.ziwei_tools import ZIWEI_TOOL_INPUTS, ZIWEI_TOOL_LEVELS


def analysis_arguments(name: str, arguments: dict) -> dict:
    """确定性转换，不猜查询目标；缺失值留给已有的聚合校验处理。"""
    level = ZIWEI_TOOL_LEVELS[name]
    target = None
    if level is not ChartLevel.NATAL:
        offset_key = {
            ChartLevel.YEARLY: "year_offset",
            ChartLevel.MONTHLY: "month_offset",
        }.get(level, "day_offset")
        if arguments.get(offset_key) is not None:
            target = {
                "kind": "relative_period",
                "unit": offset_key.removesuffix("_offset"),
                "offset": arguments[offset_key],
            }
        elif arguments.get("on_date") is not None:
            target = {"kind": "point", "on_date": arguments["on_date"]}
        elif arguments.get("date_range") is not None:
            interval = arguments["date_range"]
            target = {"kind": "bounded_range"}
            if isinstance(interval, dict):
                target.update({key: interval.get(key) for key in ("start", "end")})
        elif level in {ChartLevel.YEARLY, ChartLevel.MONTHLY}:
            target = {"kind": "calendar_period", "unit": "year", "year": arguments.get("year")}
            if level is ChartLevel.MONTHLY:
                target.update(unit="month", month=arguments.get("month"))
        else:
            target = {"kind": "point", "on_date": None}
    return {
        **{
            key: value
            for key, value in arguments.items()
            if key in ZiweiChartParameters.model_fields
        },
        "level": level,
        "target": target,
    }


def analysis_request(name: str, arguments: dict) -> ZiweiAnalysisRequest:
    """先按入口拒绝额外字段和冲突表示，再构造新的冻结计算请求。"""
    parsed = ZIWEI_TOOL_INPUTS[name].model_validate(arguments)
    return ZiweiAnalysisRequest.model_validate(
        analysis_arguments(name, parsed.model_dump(mode="json"))
    )


def public_input_error(error: ZiweiError) -> ZiweiError:
    """内部 target 路径映射回模型实际可填写的字段，不暴露不存在的通用参数。"""
    fields = {
        "target.on_date": "on_date",
        "target.year": "year",
        "target.month": "month",
        "target.start": "date_range.start",
        "target.end": "date_range.end",
    }
    return ZiweiError(
        error.code,
        str(error),
        tuple(fields.get(key, key) for key in error.fields),
        issues=tuple(
            issue.model_copy(update={"field": fields.get(issue.field, issue.field)})
            for issue in error.issues
        ),
    )


def tool_schema_error(name: str, arguments: dict, error) -> ZiweiError:
    """公开 Schema 失败时仍聚合出生和日期缺项，保持一次澄清的交互。"""
    return public_input_error(schema_error(analysis_arguments(name, arguments), error))
