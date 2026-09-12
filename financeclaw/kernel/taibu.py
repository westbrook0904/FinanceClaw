"""太卜首批工具的固定输入、时间口径及有界结果契约。"""

from datetime import date as calendar_date
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)


class TaibuError(ValueError):
    """携带可公开错误码与说明，不附带出生参数或远端异常正文。"""

    def __init__(self, code: str, message: str):
        """固定错误码和可直接交给根 Agent 的说明。"""
        self.code = code
        super().__init__(f"{code}: {message}")


class TaibuInput(BaseModel):
    """禁止未知参数，避免把模型自报地址、身份或默认值传给远端。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TaibuAlmanacInput(TaibuInput):
    """明确某日黄历；相对日期以本轮服务端时钟为锚点。"""

    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    day_offset: Annotated[StrictInt, Field(ge=-366, le=366)] | None = Field(
        default=None, description="相对本轮请求日期；今天为 0，明天为 1，与 date 二选一。"
    )
    day_master: Literal["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"] | None = None

    @model_validator(mode="after")
    def explicit_date(self):
        """拒绝缺失、冲突或不存在的日期，不采用上游默认当天。"""
        if (self.date is None) == (self.day_offset is None):
            raise ValueError("date 与 day_offset 必须且只能提供一个，请澄清查询日期")
        if self.date is not None:
            parsed = calendar_date.fromisoformat(self.date)
            if not 1900 <= parsed.year <= 2100:
                raise ValueError("date 年份必须在 1900–2100 范围内")
        return self


class TaibuBaziInput(TaibuInput):
    """中国标准时间输入；可让上游按明确经度执行一次真太阳时校正。"""

    gender: Literal["male", "female"]
    calendar_type: Literal["solar", "lunar"]
    birth_year: Annotated[StrictInt, Field(ge=1900, le=2100)]
    birth_month: Annotated[StrictInt, Field(ge=1, le=12)]
    birth_day: Annotated[StrictInt, Field(ge=1, le=31)]
    birth_hour: Annotated[StrictInt, Field(ge=0, le=23)]
    birth_minute: Annotated[StrictInt, Field(ge=0, le=59)] = Field(
        description="必须明确分钟；未知时先澄清，不能默认填 0。"
    )
    is_leap_month: StrictBool | None = Field(default=None, description="农历必须明确是否闰月。")
    time_basis: Literal["china_standard"] = Field(
        description="必须确认输入是 UTC+8 中国标准时间；海外当地钟表、夏令时或已校正太阳时不支持。"
    )
    solar_time: Literal["standard", "true_solar"] = Field(
        description="standard 按标准时间排盘；true_solar 按明确经度校正一次。"
    )
    longitude: Annotated[StrictFloat, Field(ge=-180, le=180, allow_inf_nan=False)] | None = None

    @model_validator(mode="after")
    def calendar_and_convention(self):
        """本地检查公历、农历基本边界和校正冲突；实际农历合法性由确定性服务复验。"""
        if self.calendar_type == "solar":
            calendar_date(self.birth_year, self.birth_month, self.birth_day)
            if self.is_leap_month:
                raise ValueError("公历不能设置农历闰月")
        elif self.is_leap_month is None or self.birth_day > 30:
            raise ValueError("农历必须明确是否闰月，日期最多为三十日")
        if (self.solar_time == "true_solar") != (self.longitude is not None):
            raise ValueError("真太阳时必须提供经度；标准时间不得传经度触发隐式校正")
        return self


TAIBU_TOOL_INPUTS = {"taibu_almanac": TaibuAlmanacInput, "taibu_bazi": TaibuBaziInput}


class TaibuToolResult(BaseModel):
    """把上游计算结果、限制和原始证据绑定为一个本地版本化信封。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    outcome: Literal["success", "error"]
    tool: str
    remote_tool: str
    provider: Literal["taibu"] = "taibu"
    server_version: str
    contract_hash: str
    called_at: str
    elapsed_ms: float = Field(ge=0)
    convention: dict[str, Any]
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str | None = None
    artifact_ref: dict[str, Any]
