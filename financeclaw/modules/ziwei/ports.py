"""领域所需的最小计算与历法 Port，不建设通用 Provider Registry。"""

from datetime import date
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .models import BirthContext, BirthInput, ZiweiConvention


class ZiweiEngine(Protocol):
    """紫微领域调用历法与排盘引擎所需的结构化协议。

    normalization 使用 solar_date/zone 规范化输入，计算服务使用
    natal/horoscope 取得 JSON 事实。适配器应显式消费日期、时辰和固定规则，
    不读取隐式当前时间，不把第三方对象或可变全局配置泄漏到领域层。
    XIztroEngine 是当前实现；新增引擎需要另行确认规则和结果兼容性。
    """

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
