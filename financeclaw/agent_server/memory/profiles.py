"""有类型的画像字段及可验证的显式低风险偏好，不做模型推断。"""

import re
from enum import StrEnum


class ProfileField(StrEnum):
    """首批注册字段；新增字段必须明确其验证和确认规则。"""

    LANGUAGE = "language"
    VERBOSITY = "verbosity"
    OUTPUT_FORMAT = "output_format"
    INVESTMENT_GOAL = "investment_goal"
    RISK_STATEMENT = "risk_statement"
    ACCOUNT_SCOPE = "account_scope"
    CONSTRAINT = "constraint"


LOW_RISK_VALUES = {
    ProfileField.LANGUAGE: {"zh-CN", "en"},
    ProfileField.VERBOSITY: {"concise", "detailed"},
    ProfileField.OUTPUT_FORMAT: {"table", "markdown", "bullets"},
}
_RULES = (
    (r"(?:都)?(?:请)?(?:用|使用)(?:中文)(?:回答|回复)?", ProfileField.LANGUAGE, "zh-CN"),
    (r"(?:都)?(?:请)?(?:用|使用)(?:英文|英语)(?:回答|回复)?", ProfileField.LANGUAGE, "en"),
    (
        r"(?:回答|回复)(?:都)?(?:请)?(?:简短|简洁|精简)(?:些|一些|一点)?",
        ProfileField.VERBOSITY,
        "concise",
    ),
    (r"(?:回答|回复)(?:都)?(?:请)?详细(?:些|一些|一点)?", ProfileField.VERBOSITY, "detailed"),
    (r"(?:都)?(?:请)?(?:用|使用)(?:表格)(?:回答|回复|输出)?", ProfileField.OUTPUT_FORMAT, "table"),
    (
        r"(?:都)?(?:请)?(?:用|使用)markdown(?:回答|回复|输出)?",
        ProfileField.OUTPUT_FORMAT,
        "markdown",
    ),
    (r"(?:都)?(?:请)?(?:用|使用)列表(?:回答|回复|输出)?", ProfileField.OUTPUT_FORMAT, "bullets"),
    (r"(?:please )?(?:respond|reply|answer) in chinese", ProfileField.LANGUAGE, "zh-CN"),
    (r"(?:please )?(?:respond|reply|answer) in english", ProfileField.LANGUAGE, "en"),
    (
        r"(?:please )?(?:be|keep (?:answers|responses)) (?:brief|concise)",
        ProfileField.VERBOSITY,
        "concise",
    ),
)


def explicit_preferences(text: str) -> dict[ProfileField, str]:
    """识别完整持续性指令；引用、假设、否定、疑问及未知措辞不自动生效。"""
    value = text.strip().lower().rstrip("。.!！")
    persistent = re.match(
        r"^(?:请)?(?:以后|今后|从现在开始|始终|以后请|请记住[，,:：]?|from now on[,:]? |always )",
        value,
    )
    if persistent is None:
        return {}
    clauses = re.split(r"[，,、；;]", value[persistent.end() :])
    found = {}
    for clause in clauses:
        matched = next(
            (
                (field, normalized)
                for pattern, field, normalized in _RULES
                if re.fullmatch(pattern, clause.strip())
            ),
            None,
        )
        if matched is None:
            return {}
        field, normalized = matched
        if field in found and found[field] != normalized:
            return {}
        found[field] = normalized
    return found


def validate_profile_value(field: ProfileField, value: str) -> None:
    """低风险字段只接受规范值，防止把其他事实夹带进呈现偏好。"""
    if field in LOW_RISK_VALUES and value not in LOW_RISK_VALUES[field]:
        raise ValueError(f"invalid value for profile field {field.value}")
