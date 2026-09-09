"""固定 x-iztro 0.4.0 和 tzdata 2026.3，不使用托管命理服务或全局配置。"""

import json
from datetime import date
from importlib import metadata, resources
from typing import Any
from zoneinfo import ZoneInfo

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.kernel.ziwei import BirthContext, BirthInput, ZiweiConvention

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


class XIztroEngine:
    """使用固定 x-iztro 与 tzdata 版本实现 ZiweiEngine Port。

    构造时验证依赖版本，调用时才加载排盘库；每次排盘使用独立 Astro 和
    ChartConfig，不修改第三方全局规则。晚子时的日界调整只交给引擎执行
    一次，适配器不预先加日。返回值转换为普通 JSON，隔离第三方对象生命周期。
    """

    def __init__(self) -> None:
        """版本不一致立即失败，不用新依赖冒充固定发布。"""
        for package, expected in (("x-iztro", "0.4.0"), ("tzdata", "2026.3")):
            try:
                installed = metadata.version(package)
            except metadata.PackageNotFoundError:
                raise RuntimeError(
                    f"Ziwei requires {package}=={expected}; install the ziwei extra "
                    "with `uv sync --extra ziwei` and include it in the Agent Server image."
                ) from None
            if installed != expected:
                raise RuntimeError(f"Stage 7 requires {package}=={expected}")

    def zone(self, name: str) -> ZoneInfo:
        """只从已固定的包资源打开合法 IANA 名，不接受路径或系统时区回退。"""
        if not name or any(part in {"", ".", ".."} for part in name.split("/")) or "\\" in name:
            raise ZiweiError(
                "ZIWEI_TIME_AMBIGUOUS", "请提供有效的 IANA 时区。", ("birth.place.timezone",)
            )
        try:
            with resources.files("tzdata.zoneinfo").joinpath(name).open("rb") as handle:
                return ZoneInfo.from_file(handle, key=name)
        except (OSError, ValueError):
            raise ZiweiError(
                "ZIWEI_TIME_AMBIGUOUS", "时区不在已固定的数据版本中。", ("birth.place.timezone",)
            ) from None

    def solar_date(self, birth: BirthInput) -> date:
        """农历通过实际历法回读拒绝库会忽略的非法闰月标记。"""
        from x_iztro import Astro, IztroError

        if birth.date is None or birth.calendar is None:
            raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请补充出生日期和历法。", ("birth.date",))
        try:
            if birth.calendar == "solar":
                if birth.is_leap_month:
                    raise ValueError("solar leap month")
                return date(birth.date.year, birth.date.month, birth.date.day)
            chart = Astro().by_lunar(
                birth.date.text(), 0, "male", is_leap_month=bool(birth.is_leap_month)
            )
            raw = chart.raw_dates.lunar_date
            if (raw.lunar_year, raw.lunar_month, raw.lunar_day, raw.is_leap) != (
                birth.date.year,
                birth.date.month,
                birth.date.day,
                bool(birth.is_leap_month),
            ):
                raise ValueError("lunar date did not round-trip")
            return date(*map(int, chart.solar_date.split("-")))
        except (ValueError, IztroError):
            raise ZiweiError(
                "ZIWEI_DATE_UNSUPPORTED", "出生日期或闰月标记无效，请核对历法。", ("birth.date",)
            ) from None

    def _chart(self, birth: BirthContext, convention: ZiweiConvention) -> Any:
        """库负责且仅负责一次晚子时日界处理，adapter 不提前加一天。"""
        from x_iztro import Astro, ChartConfig

        return Astro().by_solar(
            birth.solar_date.isoformat(),
            SHICHEN.index(birth.shichen),
            birth.sex_for_chart,
            fix_leap=convention.fix_leap,
            language="zh-CN",
            config=ChartConfig(
                year_divide=convention.year_divide,
                horoscope_divide=convention.horoscope_divide,
                age_divide=convention.age_divide,
                day_divide=convention.day_divide,
                algorithm=convention.algorithm,
            ),
        )

    def natal(self, birth: BirthContext, convention: ZiweiConvention) -> dict[str, Any]:
        """隔离第三方异常，返回仅含 JSON 值的完整本命结果。"""
        try:
            return json.loads(self._chart(birth, convention).to_json())
        except (ValueError, KeyError, TypeError):
            raise ZiweiError("ZIWEI_RESULT_INVALID", "排盘引擎未返回有效本命数据。") from None

    def horoscope(
        self, birth: BirthContext, convention: ZiweiConvention, target: date
    ) -> dict[str, Any]:
        """日级规则使用日期标签和明确的早子时索引，不读取引擎当前时钟。"""
        try:
            chart = self._chart(birth, convention)
            return json.loads(chart.horoscope(target.isoformat(), 0).to_json())
        except (ValueError, KeyError, TypeError):
            raise ZiweiError("ZIWEI_RESULT_INVALID", "排盘引擎未返回有效流运数据。") from None
