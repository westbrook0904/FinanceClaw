"""飞书 JSON 2.0 任务卡与有限 Schema 表单，展示和字段还原共享声明。"""

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from financeclaw.bff.application.feishu_interactions import format_interactions
from financeclaw.kernel.interactions import InteractionResponse


class UnsupportedForm(ValueError):
    """复杂 Schema 保留明确的 JSON 命令入口。"""


def plain(text):
    """构造原生纯文本标签。"""
    return {"tag": "plain_text", "content": str(text)}


def button(event_id, op, label, *, primary=False, submit=False, option=None):
    """按钮只携带固定通知视图与操作，不携带身份或权限。"""
    value = {"view": event_id, "op": op}
    if option is not None:
        value["option"] = option
    result = {
        "tag": "button",
        "name": f"{op}_{option}" if option is not None else op,
        "text": plain(label),
        "type": "primary" if primary else "default",
        "value": value,
    }
    if submit:
        result["action_type"] = "form_submit"
    return result


def fields(schema):
    """仅将有界、明确类型的顶层字段映射为表单，拒绝丢弃未知必填字段。"""
    properties = schema.get("properties", {})
    if schema.get("type") != "object" or not properties or len(properties) > 12:
        raise UnsupportedForm("unsupported object")
    if any(key in schema for key in ("oneOf", "anyOf", "allOf", "if", "patternProperties")):
        raise UnsupportedForm("conditional object")
    if set(schema.get("required", [])) - set(properties):
        raise UnsupportedForm("undeclared required fields")
    result = []
    for index, (key, spec) in enumerate(properties.items()):
        kind = spec.get("type")
        if kind not in {"string", "integer", "number", "boolean", "array"}:
            raise UnsupportedForm("unsupported field type")
        if any(name in spec for name in ("oneOf", "anyOf", "allOf", "if")):
            raise UnsupportedForm("conditional field")
        enum = (
            spec.get("enum")
            if kind == "string"
            else spec.get("items", {}).get("enum")
            if kind == "array"
            else None
        )
        if kind == "array" and spec.get("items", {}).get("type") != "string":
            raise UnsupportedForm("unsupported array")
        if enum is not None and (
            not 1 <= len(enum) <= 20 or not all(isinstance(v, str) for v in enum)
        ):
            raise UnsupportedForm("unsupported enum")
        if kind == "array" and enum is None:
            raise UnsupportedForm("array requires an enum")
        result.append((f"f{index}", key, spec, key in schema.get("required", []), enum))
    return result


def widget(name, label, spec, required, enum):
    """编辑控件位于 form 内，由提交按钮统一回传。"""
    kind = spec["type"]
    if enum is not None:
        item = {
            "tag": "multi_select_static" if kind == "array" else "select_static",
            "name": name,
            "placeholder": plain(label),
            "options": [{"text": plain(v), "value": f"o{i}"} for i, v in enumerate(enum)],
        }
        if required and kind != "array":
            item["required"] = True
        return item
    if kind == "boolean":
        return {"tag": "checker", "name": name, "text": plain(label), "checked": False}
    return {
        "tag": "input",
        "name": name,
        "label": plain(label),
        "required": required,
        "placeholder": plain("请输入"),
        "max_length": min(spec.get("maxLength", 4000), 16000),
    }


def parse_fields(schema, submitted):
    """按照冻结字段表显式还原类型；完整业务 Schema 由受理层再次校验。"""
    specs = fields(schema)
    if not isinstance(submitted, dict) or set(submitted) - {item[0] for item in specs}:
        raise ValueError("unexpected form fields")
    result = {}
    for name, key, spec, required, enum in specs:
        kind = spec["type"]
        value = submitted.get(name)
        if value is None:
            if kind == "boolean":
                value = False
            elif kind == "array":
                value = []
            elif required:
                raise ValueError(f"missing field: {key}")
            else:
                continue
        if value == "" and not required:
            continue
        if enum is not None:
            mapping = {f"o{i}": item for i, item in enumerate(enum)}
            if kind == "array":
                if not isinstance(value, list) or not all(
                    isinstance(v, str) and v in mapping for v in value
                ):
                    raise ValueError("unknown selection")
                if len(set(value)) != len(value):
                    raise ValueError("duplicate selection")
                value = [item for code, item in mapping.items() if code in value]
            elif not isinstance(value, str) or value not in mapping:
                raise ValueError("unknown selection")
            else:
                value = mapping[value]
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError("invalid boolean")
        elif kind in {"integer", "number"}:
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("invalid number")
            value = int(value) if kind == "integer" else float(value)
            if not math.isfinite(value):
                raise ValueError("non-finite number")
        elif not isinstance(value, str):
            raise ValueError("invalid text")
        result[key] = value
    return result


def interaction_elements(event_id, item):
    """由业务类型生成明确的选择、输入或审批入口。"""
    if item["status"] != "pending":
        return [{"tag": "markdown", "content": "这次提问已结束。"}]
    elements = [{"tag": "markdown", "content": item["question"]}]
    if item["kind"] == "approval":
        action = json.dumps(item["action"], ensure_ascii=False, indent=2)
        if len(action) > 4000:
            raise UnsupportedForm("approval cannot be fully reviewed")
        elements.append(
            {"tag": "markdown", "content": "待执行动作：\n```json\n" + action + "\n```"}
        )
        elements.append(
            {
                "tag": "form",
                "name": "approval",
                "elements": [
                    {
                        "tag": "input",
                        "name": "reason",
                        "label": plain("处理说明（可选）"),
                        "max_length": 2000,
                    },
                    *[
                        button(
                            event_id,
                            decision,
                            "批准执行" if decision == "approve" else "拒绝",
                            primary=decision == "approve",
                            submit=True,
                        )
                        for decision in item["allowed_decisions"]
                    ],
                ],
            }
        )
    elif item["kind"] == "choice":
        multiple = item["selection_mode"] == "multiple"
        if not multiple and len(item["options"]) <= 3 and max(map(len, item["options"])) <= 18:
            elements.extend(
                button(event_id, "choose", option, option=f"o{i}")
                for i, option in enumerate(item["options"])
            )
        else:
            elements.append(
                {
                    "tag": "form",
                    "name": "choice",
                    "elements": [
                        widget(
                            "selection",
                            "请选择" + ("（可多选）" if multiple else ""),
                            {"type": "array" if multiple else "string"},
                            True,
                            item["options"],
                        ),
                        button(event_id, "choose", "提交选择", primary=True, submit=True),
                    ],
                }
            )
    else:
        controls = [
            widget(name, spec.get("title", key), spec, required, enum)
            for name, key, spec, required, enum in fields(item["response_schema"])
        ]
        elements.append(
            {
                "tag": "form",
                "name": "answer",
                "elements": [
                    *controls,
                    button(event_id, "answer", "提交回答", primary=True, submit=True),
                ],
            }
        )
    return elements


def response_from_card(payload, value, form):
    """还原用户明确提交的决定，不从自然语言或控件名称猜测批准。"""
    item = payload["interaction"]
    op = value["op"]
    common = {"revision": item["revision"], "kind": item["kind"]}
    if op in {"approve", "reject"}:
        if item["kind"] != "approval" or op not in item["allowed_decisions"]:
            raise ValueError("decision not allowed")
        # 与展示使用同一完整性检查，复杂动作不能绕过无按钮的文本提示。
        interaction_elements("validation", item)
        if set(form) - {"reason"}:
            raise ValueError("unexpected approval fields")
        return InteractionResponse(
            **common,
            decision=op,
            action_hash=item["action_hash"],
            reason=form.get("reason") or None,
        )
    if op == "answer" and item["kind"] == "input":
        return InteractionResponse(**common, answer=parse_fields(item["response_schema"], form))
    if op == "choose" and item["kind"] == "choice":
        if set(form) - {"selection"}:
            raise ValueError("unexpected choice fields")
        options = {f"o{i}": option for i, option in enumerate(item["options"])}
        selected = value.get("option", form.get("selection"))
        if item["selection_mode"] == "multiple":
            selected = [] if selected is None else selected
            if not isinstance(selected, list) or len(set(selected)) != len(selected):
                raise ValueError("invalid multiple choice")
            answer = [options[code] for code in selected]
        else:
            answer = options[selected]
        return InteractionResponse(**common, answer=answer)
    raise ValueError("operation does not match interaction")


def render_card(event_id, payload):
    """每轮一张可更新任务卡；停止按钮随状态关闭，不承诺在途副作用回滚。"""
    status = payload["status"]
    titles = {
        "accepted": "已收到，正在处理",
        "running": "正在处理",
        "interrupted": "需要你的处理",
        "cancellation_requested": "正在停止本轮",
        "cancelled": "本轮已停止",
        "completed": "本轮已完成",
        "failed": "本轮处理失败",
    }
    ended = status in {"completed", "failed", "cancelled", "cancellation_requested"}
    elements = [{"tag": "markdown", "content": payload.get("task", "本轮任务")}]
    grant = payload["grant"]
    if not ended:
        unavailable = grant["revoked"] or payload.get("waiting_reason") == "authorization_required"
        if unavailable:
            elements.append(
                {"tag": "markdown", "content": "后台授权已撤销或过期。确认后可重新授权继续本轮。"}
            )
            elements.append(button(event_id, "authorize", "授权并继续", primary=True))
        elif payload.get("interaction"):
            try:
                controls = interaction_elements(event_id, payload["interaction"])
                if len(json.dumps(controls, ensure_ascii=False).encode()) > 18000:
                    raise UnsupportedForm("form exceeds card budget")
                elements.extend(controls)
                elements.append(
                    {
                        "tag": "markdown",
                        "content": "回答截止：" + payload["interaction"]["expires_at"],
                    }
                )
            except UnsupportedForm:
                elements.append(
                    {
                        "tag": "markdown",
                        "content": format_interactions(
                            [payload["interaction"]],
                            fallback="请通过任务 API 处理。",
                            allow_text_reply=False,
                        ),
                    }
                )
        if payload.get("last_decision") and not payload.get("interaction"):
            elements.append({"tag": "markdown", "content": payload["last_decision"]})
        deadline = (
            datetime.fromisoformat(grant["expires_at"])
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .strftime("%Y-%m-%d %H:%M:%S（北京时间）")
        )
        elements.append(
            {
                "tag": "markdown",
                "content": "授权范围：" + "、".join(grant["scopes"]) + "\n授权截至：" + deadline,
            }
        )
        if not unavailable:
            elements.append(button(event_id, "revoke", "撤销后台授权"))
        elements.append(button(event_id, "cancel", "停止本轮"))
    elif status == "cancellation_requested":
        elements.append({"tag": "markdown", "content": "停止请求已受理，正在确认执行结束。"})
    elif status == "cancelled":
        elements.append(
            {
                "tag": "markdown",
                "content": "可以发送新消息开始下一轮。已经发生的外部操作不会自动撤回。",
            }
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": plain(titles.get(status, status)), "template": "blue"},
        "body": {"elements": elements},
    }
