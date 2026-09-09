"""五个排盘工具的公开输入契约；层级由入口固定，不再使用通用 target 选择器。"""

from datetime import date
from types import MappingProxyType
from typing import Self

from pydantic import Field, model_validator

from financeclaw.kernel.ziwei import ChartLevel, ZiweiChartParameters, ZiweiModel


class ZiweiDateRange(ZiweiModel):
    """显式公历区间，不用代表日期替代整段查询。"""

    start: date | None = Field(default=None, description="起始公历日期 YYYY-MM-DD，包含当天。")
    end: date | None = Field(
        default=None, description="结束公历日期 YYYY-MM-DD，不含当天；区间不超过 366 天。"
    )


class ZiweiNatalInput(ZiweiChartParameters):
    """本命盘只需要出生资料，不接受查询日期或层级。"""


class ZiweiDatedInput(ZiweiChartParameters):
    """流运可查询某日的盘面或明确日期区间；每次只选一种日期表示。"""

    on_date: date | None = Field(
        default=None, description="查询此公历日期 YYYY-MM-DD 所在的盘面；不与期间或偏移量同时填写。"
    )
    date_range: ZiweiDateRange | None = Field(
        default=None, description="需要连续区间时填写 start/end；不与具体日期、年月或偏移量混用。"
    )

    @model_validator(mode="after")
    def one_date_form(self) -> Self:
        """拒绝互相冲突的查询表示，0 偏移量也是用户明确的选择。"""
        forms = [self.on_date is not None, self.date_range is not None]
        forms.append(any(getattr(self, key, None) is not None for key in ("year", "month")))
        forms.extend(
            getattr(self, key, None) is not None
            for key in ("year_offset", "month_offset", "day_offset")
        )
        if sum(forms) > 1:
            raise ValueError("choose only one date, calendar period, relative offset or date range")
        return self


class ZiweiDecadalInput(ZiweiDatedInput):
    """按指定日期定位所在大限，不猜测第几个大限或十年起止日期。"""

    day_offset: int | None = Field(
        default=None,
        ge=-10,
        le=10,
        examples=[0],
        description=(
            "查询今天所在的大限填 0；按 time_context.request_clock 和查询时区定位，未知留空。"
        ),
    )


class ZiweiYearlyInput(ZiweiDatedInput):
    """流年使用明确年份或相对年份，期间单位由工具固定。"""

    year: int | None = Field(
        default=None,
        ge=1901,
        le=2099,
        description="查询整个公历年份；今年或明年请改填 year_offset。",
    )
    year_offset: int | None = Field(
        default=None,
        ge=-10,
        le=10,
        examples=[0, 1, -1],
        description="0=今年整年，1=明年，-1=去年；按 time_context.request_clock 和查询时区解析。",
    )


class ZiweiMonthlyInput(ZiweiDatedInput):
    """流月使用明确年月或相对月份，不自行推算跨年偏移。"""

    year: int | None = Field(
        default=None, ge=1901, le=2099, description="明确查询月份的公历年份，与 month 一起填写。"
    )
    month: int | None = Field(
        default=None, ge=1, le=12, description="查询整个公历月份，与 year 一起填写；未知不猜测。"
    )
    month_offset: int | None = Field(
        default=None,
        ge=-10,
        le=10,
        examples=[0, 1, -1],
        description="0=本月整月，1=下月，-1=上月；按 time_context.request_clock 和查询时区解析。",
    )


class ZiweiDailyInput(ZiweiDatedInput):
    """流日使用明确日期、相对日或短日期区间。"""

    day_offset: int | None = Field(
        default=None,
        ge=-10,
        le=10,
        examples=[0, 1, -1],
        description="0=今天，1=明天，-1=昨天；按 time_context.request_clock 和查询时区解析。",
    )


ZIWEI_TOOL_INPUTS = MappingProxyType(
    {
        "ziwei_natal_chart": ZiweiNatalInput,
        "ziwei_decadal_chart": ZiweiDecadalInput,
        "ziwei_yearly_chart": ZiweiYearlyInput,
        "ziwei_monthly_chart": ZiweiMonthlyInput,
        "ziwei_daily_chart": ZiweiDailyInput,
    }
)
ZIWEI_TOOL_LEVELS = MappingProxyType(
    {
        "ziwei_natal_chart": ChartLevel.NATAL,
        "ziwei_decadal_chart": ChartLevel.DECADAL,
        "ziwei_yearly_chart": ChartLevel.YEARLY,
        "ziwei_monthly_chart": ChartLevel.MONTHLY,
        "ziwei_daily_chart": ChartLevel.DAILY,
    }
)
