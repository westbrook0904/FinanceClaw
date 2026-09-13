"""结构化缺项的合并展示保留对象、具体原因和缺少元数据时的原问题。"""

import pytest

from financeclaw.agent_server.domains.ziwei.clarification import merge_clarification_questions
from financeclaw.kernel.ziwei import ZiweiInputIssue, ZiweiTextResult


def clarification(question, fields, issues=(), subject="甲"):
    """构造不含个人资料的公开错误结果，不需要引擎或模型。"""
    return ZiweiTextResult(
        outcome="needs_clarification",
        subject_label=subject,
        question=question,
        missing_fields=fields,
        issues=tuple(
            ZiweiInputIssue(field=field, code=code, message=text) for field, code, text in issues
        ),
    )


def test_same_field_keeps_distinct_validation_reasons():
    """字段去重不丢掉第二种校验原因，也不遗漏缺项列表之外的格式错误。"""
    first = clarification(
        "请确认年份。",
        ("year",),
        (("year", "missing", "要查询的年份"),),
    )
    second = clarification(
        "请确认年份范围和解读重点。",
        ("year",),
        (
            ("year", "missing", "要查询的年份"),
            ("year", "unsupported", "年份超出可查询范围。"),
            ("year", "invalid", "年份必须使用公历。"),
            ("focus", "literal_error", "解读重点格式有误。"),
        ),
    )
    question = merge_clarification_questions([first, second, second])
    assert question.count("要查询的年份") == 1
    assert question.count("年份超出可查询范围。") == 1
    assert question.count("年份必须使用公历。") == 1
    assert "解读重点格式有误。" in question
    assert len([line for line in question.splitlines() if line.startswith("- ")]) == 2


@pytest.mark.parametrize("metadata", [(), (("year", "missing", "要查询的年份"),)])
def test_incomplete_issue_metadata_preserves_original_questions(metadata):
    """无 issues 或部分缺项未附说明时，不因结构化重排而丢掉用户需要回答的内容。"""
    first = clarification("请补充查询年份。", ("year",), (("year", "missing", "要查询的年份"),))
    second = clarification("请确认查询年份与月份。", ("year", "month"), metadata)
    assert merge_clarification_questions([first, second, second]) == (
        "请补充查询年份。\n请确认查询年份与月份。"
    )


def test_identical_question_text_does_not_hide_different_fields():
    """两个结果用了相同笼统问法时，仍按字段补全展示，不只比较整段文本。"""
    results = [
        clarification("请补充查询日期。", (field,), ((field, "missing", label),))
        for field, label in (("year", "要查询的年份"), ("month", "要查询的月份"))
    ]
    question = merge_clarification_questions(results)
    assert "要查询的年份" in question and "要查询的月份" in question


def test_different_subjects_keep_their_own_questions():
    """不同人的相同缺项分别展示；单个对象的原问法保持不变。"""
    results = [
        clarification(
            "请补充出生时间。",
            ("birth.time",),
            (("birth.time", "missing", "出生时间或明确的时辰"),),
            subject=label,
        )
        for label in ("甲", "乙")
    ]
    assert merge_clarification_questions([results[0], results[0]]) == "请补充出生时间。"
    assert merge_clarification_questions(results) == (
        "甲：请补充出生时间。\n\n乙：请补充出生时间。"
    )
