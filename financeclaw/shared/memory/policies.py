"""有类型的画像字段及可验证的显式低风险偏好，不做模型推断。"""

import json
import re

from financeclaw.shared.memory.models import ProfileField

LOW_RISK_VALUES = {
    ProfileField.LANGUAGE: {"zh-CN", "en"},
    ProfileField.VERBOSITY: {"concise", "detailed"},
    ProfileField.OUTPUT_FORMAT: {"table", "markdown", "bullets"},
}
_RULES = (
    (r"(?:都)?(?:请)?(?:用|使用)(?:中文)(?:回答|回复)?", ProfileField.LANGUAGE, "zh-CN"),
    (r"(?:都)?(?:请)?(?:用|使用)(?:英文|英语)(?:回答|回复)?", ProfileField.LANGUAGE, "en"),
    (
        r"(?:回答|回复)?(?:都)?(?:请)?(?:简短|简洁|精简)(?:些|一些|一点)?",
        ProfileField.VERBOSITY,
        "concise",
    ),
    (r"(?:回答|回复)?(?:都)?(?:请)?详细(?:些|一些|一点)?", ProfileField.VERBOSITY, "detailed"),
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


_SECRET = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/-]{12,}|\b(?:sk|rk|pk|lsv2_pt)-[A-Za-z0-9_-]{12,}\b|"
    r"(?:api[_ -]?key|password|密码|口令)\s*[:=：]\s*\S+)",
    re.I,
)
_TEMPORAL = re.compile(r"(?:current|currently|latest|now|实时|当前|最新|现在)", re.I)
_FINANCIAL_FACT = re.compile(
    r"(?:price|quote|holding|position|balance|market\s+value|exchange\s+rate|行情|股价|价格|持仓|仓位|余额|市值|汇率)",
    re.I,
)
_NON_ASSERTION = re.compile(
    r"(?:这次|本次|仅这|假设|如果|他说|她说|他人|别人|不要|不再|并非|不代表|并不|[？?“”「」]|"
    r"for this (?:answer|turn)|this time|hypothetically|suppose|someone said)",
    re.I,
)


def reject_secrets(content: str) -> None:
    """Credentials must never become a background model input or a durable fact."""
    if _SECRET.search(content):
        from financeclaw.shared.memory.models import MemoryPermissionError

        raise MemoryPermissionError("credentials and secrets cannot enter memory processing")


def validate_memory_content(content: str) -> None:
    """Keep live financial facts in their governed tools instead of durable memory."""
    reject_secrets(content)
    if _TEMPORAL.search(content) and _FINANCIAL_FACT.search(content):
        from financeclaw.shared.memory.models import MemoryPermissionError

        raise MemoryPermissionError("current financial facts cannot be persisted as memory")


def reject_nonasserted_profile(documents) -> None:
    """Do not turn temporary, quoted or hypothetical user text into a profile candidate."""
    original = [document for document in documents if document.source_kind == "user_message"]
    latest = max(original, key=lambda document: document.ref.source_seq, default=None)
    if latest is not None and _NON_ASSERTION.search(latest.content):
        from financeclaw.shared.memory.models import MemoryPermissionError

        raise MemoryPermissionError("temporary or nonasserted input cannot establish a profile")


def explicit_forget_intent(documents, memory_id: str) -> bool:
    """Require a complete original user command naming the exact deletion target."""
    patterns = (
        rf"(?:请)?(?:删除|忘记|遗忘)(?:记忆|这条记忆)?[：: ]*{re.escape(memory_id)}",
        rf"(?:please )?(?:forget|delete|remove)(?: memory)? {re.escape(memory_id)}",
    )
    return any(
        document.source_kind == "user_message"
        and any(
            re.fullmatch(pattern, document.content.strip().rstrip("。.!！"), re.I)
            for pattern in patterns
        )
        for document in documents
    )


def interaction_preferences(content: str) -> dict[ProfileField, str]:
    """Parse a complete accepted assertion together with its frozen question semantics."""
    value = json.loads(content)
    point = value.get("request", {}).get("point", {})
    response = value.get("response", {})
    if response.get("kind") not in {"input", "choice"} or _NON_ASSERTION.search(
        point.get("question", "").replace("？", "").replace("?", "")
    ):
        return {}
    answer = response.get("answer")
    if isinstance(answer, dict) and len(answer) == 1:
        field, answer = next(iter(answer.items()))
        if (
            point.get("response_schema", {}).get("properties", {}).get(field, {}).get("type")
            != "string"
        ):
            return {}
    if not isinstance(answer, str):
        return {}
    if response.get("kind") == "choice" and answer not in point.get("options", []):
        return {}
    return explicit_preferences(answer)


def source_preferences(document) -> dict[ProfileField, str]:
    """Use question-aware parsing for forms and full original text for user messages."""
    if document.source_kind == "interaction_answer":
        return interaction_preferences(document.content)
    return explicit_preferences(document.content) if document.source_kind == "user_message" else {}
