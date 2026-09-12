"""Immutable proposal cards whose decisions are independent of native interrupts."""


def render_memory_card(event_id, payload):
    """Display the exact candidate content bound into both server-validated buttons."""
    buttons = []
    for decision, label in (("approve", "确认"), ("reject", "拒绝")):
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if decision == "approve" else "default",
                "value": {
                    "view": event_id,
                    "op": "memory_candidate.decide",
                    "candidate_id": payload["candidate_id"],
                    "revision": payload["revision"],
                    "content_hash": payload["content_hash"],
                    "decision": decision,
                },
            }
        )
    operation = {"create": "保存", "update": "更新", "forget": "遗忘"}[payload["operation"]]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"待确认：{operation}长期记忆"},
            "template": "orange",
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": payload["content"]},
                },
                {
                    "tag": "markdown",
                    "content": (
                        f"适用范围：{payload['scope_type']} / "
                        f"{payload.get('scope_id') or '当前用户'}"
                        "\n确认后生效，可在记忆管理中查看、纠正或遗忘。"
                    ),
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [button]}
                        for button in buttons
                    ],
                },
            ]
        },
    }
