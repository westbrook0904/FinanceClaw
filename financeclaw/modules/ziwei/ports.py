"""领域所需的最小计算与历法 Port，不建设通用 Provider Registry。"""

from datetime import date
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .models import BirthContext, BirthInput, ZiweiConvention


class ZiweiEngine(Protocol):
    """适配器必须显式接收日期、时辰和固定规则，不使用进程当前时间。"""

    def solar_date(self, birth: BirthInput) -> date:
        """校验并将用户日期转为公历；农历闰月必须做反向验证。"""
        ...

    def natal(self, birth: BirthContext, convention: ZiweiConvention) -> dict[str, Any]:
        """取得完整本命 JSON；不返回第三方对象。"""
        ...

    def horoscope(
        self, birth: BirthContext, convention: ZiweiConvention, target: date
    ) -> dict[str, Any]:
        """返回目标日的所有流运层级，调用者按需要选择，不取此刻默认值。"""
        ...

    def zone(self, name: str) -> ZoneInfo:
        """从固定 tzdata 版本取时区，不能隐式改用系统数据库。"""
        ...
