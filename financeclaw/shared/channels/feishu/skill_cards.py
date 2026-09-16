"""新建技能任务的固定表单视图；选择控件不发起任务或模型请求。"""

from financeclaw.shared.channels.feishu.cards import INPUT_MAX_LENGTH, button, plain


class SkillFormError(ValueError):
    """面向用户的表单纠正提示，只由平台使用固定文案构造。"""


def render_skill_form(event_id, payload):
    """复用 JSON 2.0 表单与提交按钮，选项对应服务端冻结的完整发布引用。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": "新建技能任务"}},
        "header": {"title": plain("FinanceClaw · 新建技能任务"), "template": "blue"},
        "body": {
            "elements": [
                {
                    "tag": "form",
                    "name": "skill_task_form",
                    "elements": [
                        {"tag": "markdown", "content": "选择技能"},
                        {
                            "tag": "select_static",
                            "name": "skill",
                            "required": True,
                            "width": "fill",
                            "placeholder": plain("请选择技能"),
                            "options": [
                                {"text": plain(item["label"]), "value": f"o{index}"}
                                for index, item in enumerate(payload["skills"])
                            ],
                        },
                        {"tag": "markdown", "content": "任务描述"},
                        {
                            "tag": "input",
                            "name": "task_description",
                            "required": True,
                            "input_type": "multiline_text",
                            "width": "fill",
                            "max_length": INPUT_MAX_LENGTH,
                            "placeholder": plain("请描述希望完成的任务，并提供必要材料。"),
                        },
                        {"tag": "markdown", "content": "选择仅对本次任务生效。"},
                        {
                            "tag": "column_set",
                            "horizontal_align": "right",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "auto",
                                    "elements": [
                                        button(
                                            event_id,
                                            "skill_start",
                                            "开始执行",
                                            primary=True,
                                            submit=True,
                                        )
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ]
        },
    }


def skill_form_selection(payload, form):
    """仅解析卡片中的两个必填字段，不接受客户端提供的身份、权限或版本。"""
    if not isinstance(form, dict) or set(form) != {"skill", "task_description"}:
        raise SkillFormError("请选择技能并填写任务描述。")
    options = {f"o{index}": item for index, item in enumerate(payload["skills"])}
    selected, task = form["skill"], form["task_description"]
    if not isinstance(selected, str) or selected not in options:
        raise SkillFormError("请选择这张表单提供的技能。")
    if not isinstance(task, str) or not task.strip() or len(task) > INPUT_MAX_LENGTH:
        raise SkillFormError("请填写 1 至 1000 字的任务描述。")
    return options[selected], task.strip()
