"""确定性盘面服务：一次调用产出所需层级，并按实际事实变化拆分日期区间。"""

from datetime import timedelta
from hashlib import sha256
from typing import Any

from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.domains.ziwei.normalization import canonical
from financeclaw.agent_server.domains.ziwei.ports import ZiweiEngine
from financeclaw.kernel.ziwei import (
    LEVELS,
    BirthContext,
    ChartCalculation,
    ChartFact,
    ChartLevel,
    ChartProjection,
    ChartSegment,
    Focus,
    ResolvedTarget,
    ZiweiConvention,
)


def _stars(stars: list[dict[str, Any]]) -> list[list[str]]:
    """星曜紧凑表示固定为 key、名称、亮度、四化；所属层级由事实声明。"""
    return [[s["key"], s["name"], s.get("brightness", ""), s.get("mutagen", "")] for s in stars]


def _fact(
    layer: ChartLevel, value: object, palace: int | None = None, *, auxiliary=False
) -> ChartFact:
    """内容寻址仅针对盘面事实，生日缓存指纹另用 HMAC。"""
    encoded = canonical(value)
    prefix = "aux" if auxiliary else "core"
    key = sha256(encoded.encode()).hexdigest()[:20]
    return ChartFact(
        fact_id=f"{layer.value}.{prefix}.{key}",
        layer=layer,
        palace_index=palace,
        value_json=encoded,
    )


class ZiweiCalculationService:
    """把固定历法引擎结果转换为可引用、可复现的盘面事实。

    只依赖 ZiweiEngine Port 和版本化规则：一次读取本命盘，再按日期读取
    所需流运层级，将相同事实覆盖的相邻日期合并为半开区间。max_segments
    限制事实变化产生的分段数，超限要求缩小问题，不截断成功结果。

    返回完整 ChartCalculation；主题裁剪由 project 完成，身份授权与
    Artifact 写入由应用层完成，本服务不访问数据库、模型或用户缓存。
    """

    def __init__(
        self, engine: ZiweiEngine, convention: ZiweiConvention, *, max_segments=32
    ) -> None:
        """固定 adapter 和 Convention；预算随发布指纹固定。"""
        self.engine = engine
        self.convention = convention
        self.max_segments = max_segments

    def calculate_snapshot(
        self, birth: BirthContext, target: ResolvedTarget | None, level: ChartLevel
    ) -> ChartCalculation:
        """计算完整事实，最多扫描 366 日；不是让模型调用 366 次工具。"""
        if birth.convention_ref != self.convention.ref:
            raise ZiweiError("ZIWEI_CONVENTION_UNSUPPORTED", "出生上下文和计算规则版本不一致。")
        if (level is ChartLevel.NATAL) != (target is None):
            raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "排盘层级和查询时间不匹配。")
        if target and (target.start < birth.solar_date or target.end.year > 2100):
            raise ZiweiError("ZIWEI_DATE_UNSUPPORTED", "流运查询必须在出生之后且处于支持范围。")
        if target and level is ChartLevel.DAILY and (target.end - target.start).days > 31:
            raise ZiweiError("ZIWEI_RANGE_LIMIT", "单次逐日查询最多 31 天，请缩小区间。")
        try:
            return self._calculate(birth, target, level)
        except (KeyError, TypeError, IndexError):
            raise ZiweiError("ZIWEI_RESULT_INVALID", "排盘结果未满足固定字段与层级契约。") from None

    def _calculate(
        self, birth: BirthContext, target: ResolvedTarget | None, level: ChartLevel
    ) -> ChartCalculation:
        """本命复用一次，流运按目标层级事实分段并去重。"""
        natal = self.engine.natal(birth, self.convention)
        palaces = natal["palaces"]
        if len(palaces) != 12 or [p["index"] for p in palaces] != list(range(12)):
            raise ZiweiError("ZIWEI_RESULT_INVALID", "本命盘必须包含按索引排列的十二宫。")
        facts = [
            _fact(
                ChartLevel.NATAL,
                {
                    key: natal[key]
                    for key in (
                        "soul",
                        "body",
                        "fiveElementsClass",
                        "earthlyBranchOfSoulPalace",
                        "earthlyBranchOfBodyPalace",
                        "chineseDate",
                    )
                },
            )
        ]
        for palace in palaces:
            core = {
                "index": palace["index"],
                "name": palace["name"],
                "name_key": palace["nameKey"],
                "stem": palace["heavenlyStem"],
                "branch": palace["earthlyBranch"],
                "is_body": palace["isBodyPalace"],
                "decadal_ages": palace["decadal"]["range"],
                "stars": _stars(palace["majorStars"] + palace["minorStars"]),
                "surrounded_indices": [(palace["index"] + n) % 12 for n in (0, 4, 6, 8)],
            }
            facts.append(_fact(ChartLevel.NATAL, core, palace["index"]))
            facts.append(_fact(ChartLevel.NATAL, palace, palace["index"], auxiliary=True))
        segments: list[ChartSegment] = []
        unique = {f.fact_id: f for f in facts}
        if target:
            day = target.start
            while day < target.end:
                horoscope = self.engine.horoscope(birth, self.convention, day)
                selected = []
                for current in LEVELS[1 : LEVELS.index(level) + 1]:
                    raw = horoscope[current.value]
                    index = raw["index"]
                    if not 0 <= index <= 11 or len(raw["palaceNames"]) != 12:
                        raise ZiweiError(
                            "ZIWEI_DATE_UNSUPPORTED", "目标日期尚未起限或超出有效流运范围。"
                        )
                    data = {
                        "index": index,
                        "stem": raw["heavenlyStem"],
                        "branch": raw["earthlyBranch"],
                        "palace_names": raw["palaceNames"],
                        "mutagens_lu_quan_ke_ji": raw["mutagen"],
                        "stars_by_palace": [_stars(stars) for stars in raw["stars"]],
                    }
                    if current is ChartLevel.DECADAL:
                        ages = palaces[index]["decadal"]["range"]
                        if not ages[0] <= horoscope["age"]["nominalAge"] <= ages[1]:
                            raise ZiweiError(
                                "ZIWEI_DATE_UNSUPPORTED", "引擎尚未提供此日期的有效大限。"
                            )
                        data["nominal_age_range"] = ages
                    selected.extend((_fact(current, data), _fact(current, raw, auxiliary=True)))
                for fact in selected:
                    unique[fact.fact_id] = fact
                ids = tuple(f.fact_id for f in selected)
                end = day + timedelta(days=1)
                if segments and segments[-1].fact_ids == ids:
                    segments[-1] = segments[-1].model_copy(update={"end": end})
                else:
                    segments.append(ChartSegment(start=day, end=end, fact_ids=ids))
                if len(segments) > self.max_segments:
                    raise ZiweiError("ZIWEI_RANGE_LIMIT", "规则分段超过本次预算，请缩小查询区间。")
                day = end
        stable_target = (
            target.model_dump(mode="json", exclude={"request_clock"}) if target else None
        )
        identity = [
            birth.fingerprint,
            self.convention.model_dump(mode="json"),
            stable_target,
            level.value,
        ]
        return ChartCalculation(
            chart_id="ziwei-" + sha256(canonical(identity).encode()).hexdigest(),
            birth_fingerprint=birth.fingerprint,
            convention=self.convention,
            level=level,
            target=target,
            facts=tuple(unique.values()),
            segments=tuple(segments),
            warnings=birth.warnings,
        )


def project(calculation: ChartCalculation, focus: Focus, *, max_bytes: int) -> ChartProjection:
    """先丢弃明确标识的原始辅助字段；仍超限则失败，不改写已引用事实。"""
    import json

    core = [fact for fact in calculation.facts if ".core." in fact.fact_id]
    if focus != "overall":
        palace_key = {
            "career": "careerPalace",
            "relationship": "spousePalace",
            "wealth": "wealthPalace",
        }[focus]
        indexes: set[int] = set()
        for fact in core:
            if fact.layer is ChartLevel.NATAL and fact.palace_index is not None:
                data = json.loads(fact.value_json)
                if data["name_key"] in {palace_key, "soulPalace"} or data["is_body"]:
                    indexes.update(data["surrounded_indices"])
        # 动态宫位可能移动，相关本命宫位也必须保留。
        display = {"career": "官禄", "relationship": "夫妻", "wealth": "财帛"}[focus]
        for fact in core:
            if fact.layer is not ChartLevel.NATAL:
                names = json.loads(fact.value_json)["palace_names"]
                for index in (names.index(display), names.index("命宫")):
                    indexes.update((index + delta) % 12 for delta in (0, 4, 6, 8))
        core = [f for f in core if f.palace_index is None or f.palace_index in indexes]
    ids = {f.fact_id for f in core}
    projection = ChartProjection(
        chart_id=calculation.chart_id,
        birth_fingerprint=calculation.birth_fingerprint,
        convention_ref=calculation.convention.ref,
        engine_ref=calculation.convention.engine,
        level=calculation.level,
        target=calculation.target,
        facts=tuple(core),
        segments=tuple(
            s.model_copy(update={"fact_ids": tuple(i for i in s.fact_ids if i in ids)})
            for s in calculation.segments
        ),
        omitted_fact_ids=tuple(f.fact_id for f in calculation.facts if f.fact_id not in ids),
        warnings=(
            *calculation.warnings,
            "投影未包含完整辅助星曜／神煞字段；不得据此认定未展示的格局。",
        ),
    )
    if len(projection.model_dump_json().encode()) > max_bytes:
        raise ZiweiError(
            "ZIWEI_CONTEXT_BUDGET_EXCEEDED", "所需盘面证据超出上下文预算，请缩小区间。"
        )
    return projection
