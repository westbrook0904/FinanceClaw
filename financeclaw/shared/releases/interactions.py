"""根任务统一澄清的固定回答契约。"""

from financeclaw.kernel.interactions import InteractionPoint

ROOT_CLARIFICATION = InteractionPoint(
    point_id="clarification",
    kind="input",
    question="请补充或确认本次任务所需的信息。",
    response_schema={
        "type": "object",
        "properties": {
            "text": {"title": "你的回答", "type": "string", "minLength": 1, "maxLength": 8000}
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)
CLARIFICATION_TOOL = "request_user__clarification"
