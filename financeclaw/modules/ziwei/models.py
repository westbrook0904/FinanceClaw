"""紫微输入、规范化快照和结果契约；模型不能指定执行身份或修改规则。"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ZiweiModel(BaseModel):
    """禁止额外字段，以 tuple 和冻结子模型保持深层业务值不可变。"""

    # tuple、冻结子模型和 JSON 字符串共同约束嵌套值；新增可变容器时不能
    # 仅依赖 frozen=True，它只阻止字段重新赋值，不会冻结任意 dict/list 内容。
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChartLevel(StrEnum):
    """分析层级顺序，不表示五次外部调用依赖。"""

    # LEVELS 复用此声明顺序判断允许的最深层级；流运计算包含所需祖先层级。
    NATAL = "natal"
    DECADAL = "decadal"
    YEARLY = "yearly"
    MONTHLY = "monthly"
    DAILY = "daily"


LEVELS = tuple(ChartLevel)
Focus = Literal["overall", "career", "relationship", "wealth"]
Shichen = Literal[
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
    "zi",
]


class CalendarDate(ZiweiModel):
    """不使用公历 date 校验农历，避免错误拒绝合法农历二月三十。"""

    # 此处只验证数字范围；具体历法与闰月合法性由 ZiweiEngine.solar_date 校验。
    year: int = Field(ge=1901, le=2099)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)

    def text(self) -> str:
        """输出不带隐式时区的历法日期。"""
        return f"{self.year}-{self.month}-{self.day}"


class BirthTime(ZiweiModel):
    """保留实际时间精度；区间为同一民用日期内的闭区间。"""

    # clock 表示 HH:MM；range 使用 clock/end；shichen 使用独立的时辰标签。
    # kind 已选择但值缺失时允许进入 preflight，统一返回需要澄清的字段。
    kind: Literal["clock", "shichen", "range", "unknown"] = "unknown"
    clock: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shichen: Shichen | None = None
    end: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    # 夏令时回拨产生两个相同民用时刻时，0/1 选择第一次/第二次出现。
    fold: Literal[0, 1] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """拒绝相互矛盾的时间表示；缺资料仍允许进入澄清。"""
        if self.kind == "unknown" and any(
            v is not None for v in (self.clock, self.shichen, self.end)
        ):
            raise ValueError("unknown time cannot contain a clock or shichen")
        if self.kind == "shichen" and (self.clock is not None or self.end is not None):
            raise ValueError("shichen cannot contain a clock range")
        if self.kind in {"clock", "range"} and self.shichen is not None:
            raise ValueError("clock and shichen are mutually exclusive")
        if self.kind == "clock" and self.end is not None:
            raise ValueError("clock does not accept a range end")
        if self.kind in {"shichen", "unknown"} and self.fold is not None:
            raise ValueError("fold requires a civil clock or range")
        return self


class BirthPlace(ZiweiModel):
    """只收地名和必要位置资料，不需要街道地址。"""

    name: str = Field(min_length=1, max_length=160)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    # timezone 使用 IANA 名称；坐标不等于已确认时区，也不自动启用真太阳时。
    timezone: str | None = Field(default=None, max_length=64)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)


class BirthInput(ZiweiModel):
    """允许缺失字段，确定性 preflight 一次返回所有需要补充的资料。"""

    calendar: Literal["solar", "lunar"] | None = None
    date: CalendarDate | None = None
    # None 表示尚未确认，False 表示明确非闰月；农历输入需保留这个区别。
    is_leap_month: bool | None = None
    time: BirthTime = Field(default_factory=BirthTime)
    time_basis: Literal["civil", "apparent_solar"] | None = None
    place: BirthPlace | None = None
    # 只用于固定排盘算法的参数，不从姓名、标签或模型猜测。
    sex_for_chart: Literal["male", "female"] | None = None


class TargetSelector(ZiweiModel):
    """区分时点、历法区间、相对区间，全部相对时间由服务端固定。"""

    # on_date、year/month/unit、unit/offset、start/end 分别对应下列四种选择。
    # 这里只描述请求；resolve_target 再依据固定 request_clock 生成绝对区间。
    kind: Literal["point", "calendar_period", "relative_period", "bounded_range"]
    calendar: Literal["solar", "lunar"] = "solar"
    on_date: date | None = None
    year: int | None = Field(default=None, ge=1901, le=2099)
    month: int | None = Field(default=None, ge=1, le=12)
    is_leap_month: bool | None = None
    unit: Literal["year", "month", "day"] | None = None
    offset: int = Field(default=0, ge=-10, le=10)
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """禁止携带会被忽略的矛盾日期参数。"""
        allowed = {
            "point": {"on_date"},
            "calendar_period": {"year", "month", "unit", "calendar", "is_leap_month"},
            "relative_period": {"unit", "offset"},
            "bounded_range": {"start", "end"},
        }[self.kind]
        for field in ("on_date", "year", "month", "is_leap_month", "unit", "start", "end"):
            if getattr(self, field) is not None and field not in allowed:
                raise ValueError(f"{field} is not valid for {self.kind}")
        if self.offset and "offset" not in allowed:
            raise ValueError("offset is only valid for relative_period")
        if self.calendar != "solar" and "calendar" not in allowed:
            raise ValueError("this selector requires solar calendar")
        if self.calendar == "solar" and self.is_leap_month is not None:
            raise ValueError("solar periods cannot specify lunar leap months")
        return self


class ZiweiAnalysisRequest(ZiweiModel):
    """根 Agent 的领域参数；上下文引用仍使用外层 handoff 的授权引用机制。"""

    question: str = Field(default="", max_length=4000)
    # 标签仅用于展示；授权主体取 ExecutionContext，不能由此标签决定归属。
    subject_label: str = Field(default="本次排盘对象", min_length=1, max_length=80)
    mode: Literal["chart_only", "interpretation"] = "interpretation"
    birth: BirthInput = Field(default_factory=BirthInput)
    target: TargetSelector | None = None
    # level 是本次委派允许的最深层级；focus 只筛选展示事实，不改变完整盘面身份。
    level: ChartLevel = ChartLevel.NATAL
    focus: Focus = "overall"


class ZiweiConvention(ZiweiModel):
    """固定候选规则；尚未批准为产品默认，不能就地替换版本。"""

    # Literal 字段共同固定历法、算法和依赖口径；改变口径应发布新版本，
    # 并使执行快照能够识别变化，不能通过请求参数覆盖这些值。
    convention_id: Literal["x-iztro-civil-candidate"] = "x-iztro-civil-candidate"
    version: Literal["1.0.0"] = "1.0.0"
    engine: Literal["x-iztro@0.4.0"] = "x-iztro@0.4.0"
    tzdata: Literal["2026.3"] = "2026.3"
    time_basis: Literal["civil"] = "civil"
    year_divide: Literal["normal"] = "normal"
    horoscope_divide: Literal["normal"] = "normal"
    age_divide: Literal["normal"] = "normal"
    day_divide: Literal["forward"] = "forward"
    algorithm: Literal["default"] = "default"
    fix_leap: Literal[True] = True
    target_basis: Literal["local-calendar-label"] = "local-calendar-label"

    @property
    def ref(self) -> str:
        """用于缓存、发布快照和用户结果的规则引用。"""
        return f"{self.convention_id}@{self.version}"


class BirthContext(ZiweiModel):
    """由可信 preflight 生成，不暴露在模型可填写的 Tool Schema 中。"""

    solar_date: date
    shichen: Shichen
    sex_for_chart: Literal["male", "female"]
    timezone: str
    # 时辰或区间可能无法唯一定位 UTC 时刻；None 保留精度，不伪造时间点。
    utc_instant: datetime | None = None
    time_precision: Literal["clock", "shichen", "range"]
    # fingerprint 绑定规范化出生资料、规则和归属；owner_fingerprint 单独
    # 校验 tenant/subject。二者使用 HMAC，key_version 标识密钥版本而非密钥。
    fingerprint: str
    owner_fingerprint: str
    key_version: str
    convention_ref: str
    warnings: tuple[str, ...] = ()


class ResolvedTarget(ZiweiModel):
    """半开民用日期区间；日级口径不伪造一个代表时刻覆盖整个区间。"""

    # [start, end) 按 timezone 的民用日期解释；request_clock 记录相对日期
    # 解析基准，重放不得改为当前时刻。它不参与确定性盘面内容身份计算。
    start: date
    end: date
    timezone: str
    request_clock: datetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        """只接受非空、最多一年左右且已固定绝对时间的目标。"""
        if not 0 < (self.end - self.start).days <= 366:
            raise ValueError("target range must contain 1 to 366 days")
        if self.request_clock.tzinfo is None:
            raise ValueError("request_clock must be timezone-aware")
        return self


class ChartFact(ZiweiModel):
    """每个事实的 value_json 是校验后的确定性 JSON，不是第三方自由文本。"""

    # fact_id 包含层级、core/aux 类型与内容摘要；引用需再加 chart_id 前缀。
    fact_id: str
    layer: ChartLevel
    # None 表示整盘或整层事实；0～11 对应引擎固定的十二宫索引。
    palace_index: int | None = Field(default=None, ge=0, le=11)
    value_json: str


class ChartSegment(ZiweiModel):
    """相同层级事实的连续日期覆盖，不跨规则变化合并。"""

    # [start, end) 内事实引用保持相同；fact_ids 指向同一计算结果的 facts。
    start: date
    end: date
    fact_ids: tuple[str, ...]


class ChartCalculation(ZiweiModel):
    """完整确定性事实，无存储副作用；不同主题共享同一完整盘面身份。"""

    # chart_id 由出生指纹、规则、层级和目标区间生成，与展示主题无关；
    # 事实各自使用 fact_id 寻址，完整制品另有内容校验摘要。
    chart_id: str
    birth_fingerprint: str
    convention: ZiweiConvention
    level: ChartLevel
    target: ResolvedTarget | None
    # 包含核心及辅助事实；投影可以省略辅助事实，持久化快照保留完整集合。
    facts: tuple[ChartFact, ...]
    segments: tuple[ChartSegment, ...]
    warnings: tuple[str, ...] = ()


class ArtifactReference(ZiweiModel):
    """对模型只暴露内部 Artifact ID 和完整性信息，不含存储路径。"""

    # 紫微结果中的精简引用；与 kernel 的通用制品响应契约用途不同。
    # 内容读取仍由 ArtifactService 复验归属，持有 ID 本身不是访问授权。
    artifact_id: str
    content_hash: str
    size_bytes: int


class ChartProjection(ZiweiModel):
    """可独立解析的有界事实集；不会按字符截断 JSON。"""

    # 仅此处 facts 中实际展示的事实可用于模型引用；完整内容可由 artifact 追溯。
    chart_id: str
    birth_fingerprint: str
    convention_ref: str
    engine_ref: str
    level: ChartLevel
    target: ResolvedTarget | None
    facts: tuple[ChartFact, ...]
    segments: tuple[ChartSegment, ...]
    # 记录裁剪范围，避免将“未展示”误读成“盘面不存在”。
    omitted_fact_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    artifact: ArtifactReference | None = None


class Interpretation(ZiweiModel):
    """每条传统解释必须引用已实际展示给模型的事实。"""

    topic: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1500)
    # 引用格式为 chart_id/fact_id，ZiweiAgentResult 校验其属于 charts_used。
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=24)


class InterpretationDraft(ZiweiModel):
    """finalization 只生成表达与引用，不能自报 Chart 或规则版本。"""

    # 这是模型结构化输出的 Schema；可信图节点另行补入盘面、版本和执行结果。
    answer_summary: str = Field(min_length=1, max_length=2000)
    interpretations: tuple[Interpretation, ...] = Field(min_length=1, max_length=8)


class ZiweiAgentResult(ZiweiModel):
    """外层 completed 不等于完成解读；澄清和不支持也是合法终态。"""

    schema_version: Literal[1] = 1
    # outcome 描述领域结果，外层 DelegationResult.status 描述执行是否结束。
    # 父 Agent 必须保留澄清字段或不支持原因，不能把 completed 改写为成功解读。
    outcome: Literal["answer", "chart_only", "needs_clarification", "unsupported"]
    question: str = ""
    subject_label: str = "本次排盘对象"
    answer_summary: str = ""
    interpretations: tuple[Interpretation, ...] = ()
    charts_used: tuple[ChartProjection, ...] = ()
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error_code: str | None = None

    @model_validator(mode="after")
    def valid_outcome(self) -> Self:
        """不允许无盘成功，或成功引用不存在／不同对象的盘面事实。"""
        if self.outcome == "needs_clarification" and (not self.question or not self.missing_fields):
            raise ValueError("clarification requires question and missing_fields")
        if self.outcome == "unsupported" and (not self.warnings or not self.error_code):
            raise ValueError("unsupported requires warnings and error_code")
        if self.outcome in {"answer", "chart_only"} and not self.charts_used:
            raise ValueError("successful result requires calculated charts")
        if self.outcome == "answer" and (not self.answer_summary or not self.interpretations):
            raise ValueError("answer requires an evidence-backed interpretation")
        identities = {(c.birth_fingerprint, c.convention_ref) for c in self.charts_used}
        if len(identities) > 1:
            raise ValueError("result cannot mix birth identities or conventions")
        refs = {f"{c.chart_id}/{f.fact_id}" for c in self.charts_used for f in c.facts}
        if any(ref not in refs for item in self.interpretations for ref in item.evidence_refs):
            raise ValueError("interpretation references unavailable evidence")
        if self.outcome in {"unsupported", "needs_clarification"} and self.interpretations:
            raise ValueError("incomplete requests cannot contain interpretations")
        return self
