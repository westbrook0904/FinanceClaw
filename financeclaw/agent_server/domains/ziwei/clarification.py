"""将同批紫微校验问题按对象与字段合并，仅生成展示文本，不改写工具参数。"""

from collections.abc import Sequence

from financeclaw.agent_server.domains.ziwei.validation import LABELS
from financeclaw.kernel.ziwei import ZiweiTextResult

_FIELD_LABELS = {
    **LABELS,
    "year": LABELS["target.year"],
    "month": LABELS["target.month"],
    "on_date": LABELS["target.on_date"],
    "date_range.start": LABELS["target.start"],
    "date_range.end": LABELS["target.end"],
    "focus": "解读重点",
}


def _subject_question(results: Sequence[ZiweiTextResult]) -> str:
    """同对象每个字段展示一次，保留不同校验原因；无完整结构化说明时保留原问法。"""
    first = results[0]
    questions = tuple(dict.fromkeys(result.question for result in results))
    if all(
        (result.question, result.missing_fields, result.issues)
        == (first.question, first.missing_fields, first.issues)
        for result in results
    ):
        return first.question
    if any(
        not set(result.missing_fields).issubset(
            issue.field for issue in result.issues if issue.message.strip()
        )
        for result in results
    ):
        return "\n".join(questions)
    fields: dict[str, list[str]] = {}
    for result in results:
        for issue in result.issues:
            messages = fields.setdefault(issue.field, [])
            if issue.message not in messages:
                messages.append(issue.message)
    lines = []
    for field, messages in fields.items():
        label = _FIELD_LABELS.get(field, field)
        # 缺项说明本身就是字段名称时直接展示；其他原因保留字段归属及全部说明。
        details = [message for message in messages if message != label]
        lines.append(f"- {label}：{'；'.join(details)}" if details else f"- {label}")
    return "请补充或确认以下资料：\n" + "\n".join(lines)


def merge_clarification_questions(results: Sequence[ZiweiTextResult]) -> str:
    """不同对象分别列问；同对象的本命、流年等工具缺项使用同一字段清单。"""
    subjects: dict[str, list[ZiweiTextResult]] = {}
    for result in results:
        subjects.setdefault(result.subject_label, []).append(result)
    questions = [(label, _subject_question(items)) for label, items in subjects.items()]
    if len(questions) == 1:
        return questions[0][1]
    return "\n\n".join(
        f"{label}：{question}" if label else question for label, question in questions
    )
