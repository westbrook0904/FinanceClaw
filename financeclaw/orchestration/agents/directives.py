"""调用偏好指令的解析与槽位评估。

属于 orchestration.agents 的基础模块：把用户消息中的 ``/tool``、``/workflow``、
``/agent`` 指令解析为结构化 InvocationDirective，并依据工具 schema 评估其参数
槽位是否齐备合法；指令表达偏好，不构成身份或权限。

"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

# 指令匹配正则：``/<kind> <resource_id> [payload]``，resource_id 为 1-128 位
# 安全字符（字母数字与 ._:-），payload 可跨行。
_DIRECTIVE = re.compile(
    r"^/(?P<kind>tool|workflow|agent)\s+"
    r"(?P<resource>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})"
    r"(?:\s+(?P<payload>.*))?$",
    re.DOTALL,
)


class InvocationKind(StrEnum):
    """指令指向的资源种类，决定能力名的包装方式与提示语义。"""

    # 直接调用某个治理工具，能力名即工具 id。
    TOOL = "tool"
    # 把工作流作为整体能力委托调用，能力名为 delegate_workflow__<id>。
    WORKFLOW = "workflow"
    # 把领域 Agent 作为整体能力委托调用，能力名为 delegate_agent__<id>。
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class InvocationDirective:
    """一条解析后的显式调用偏好指令。

    使用场景：由 parse_invocation_directive 从用户消息解析产生，供指令中间件
    与工具治理中间件判断调用意图、收敛候选能力并校验参数槽位。

    Attributes:
        kind: 指令种类（tool、workflow 或 agent）。
        resource_id: 指令指向的资源标识，如工具 id 或领域 Agent id。
        payload: 指令后附的原始参数文本；未附参数时为 None。
        arguments: payload 以 JSON 对象解析成功的参数字典；解析失败或非 JSON
            时为 None。
        parse_error: payload 解析失败原因；解析成功或无 payload 时为 None。

    """

    kind: InvocationKind
    resource_id: str
    payload: str | None = None
    arguments: dict[str, Any] | None = None
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class SlotAssessment:
    """依据工具 schema 对指令参数槽位的评估结果。

    使用场景：由 assess_tool_slots 产出；指令中间件据此决定直接调用、追问
    缺失槽位还是禁止本回合调用工具。

    Attributes:
        arguments: 校验通过后的最终参数字典；未通过校验时为 None。schema 非
            Pydantic 模型时可能原样透传指令参数。
        missing_fields: 校验判定的缺失必填字段名列表。
        validation_errors: 校验判定的字段级错误描述列表。

    """

    arguments: dict[str, Any] | None
    missing_fields: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """槽位是否齐备且合法：参数已产出且无校验错误时为 True。"""
        return self.arguments is not None and not self.validation_errors


def parse_invocation_directive(message: str) -> InvocationDirective | None:
    """把用户消息解析为调用偏好指令；不匹配指令格式时返回 None。

    Args:
        message: 原始用户消息文本。

    Returns:
        InvocationDirective | None: 解析结果；payload 为 JSON 对象时填充
            arguments，解析失败时记录 parse_error。

    """
    match = _DIRECTIVE.fullmatch(message.strip())
    if match is None:
        return None
    payload = match.group("payload")
    payload = payload.strip() if payload is not None else None
    arguments: dict[str, Any] | None = None
    parse_error: str | None = None
    # payload 以 "{" 开头时按 JSON 解析，仅接受对象，其余情形保留原文。
    if payload and payload.startswith("{"):
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict):
                parse_error = "JSON arguments must be an object"
            else:
                arguments = decoded
        except json.JSONDecodeError as exc:
            parse_error = f"invalid JSON arguments at character {exc.pos}"
    return InvocationDirective(
        kind=InvocationKind(match.group("kind")),
        resource_id=match.group("resource"),
        payload=payload,
        arguments=arguments,
        parse_error=parse_error,
    )


def assess_tool_slots(tool: BaseTool, directive: InvocationDirective) -> SlotAssessment:
    """依据工具 schema 评估指令参数槽位是否齐备且合法。

    Args:
        tool: 指令指向的能力对应的工具实例。
        directive: 已解析的调用偏好指令。

    Returns:
        SlotAssessment: 槽位评估结果；缺失字段与校验错误分别记录在
            missing_fields 与 validation_errors 中。

    """
    # 1. 指令 payload 解析失败时，直接把解析错误作为校验错误返回。
    if directive.parse_error is not None:
        return SlotAssessment(arguments=None, validation_errors=(directive.parse_error,))
    schema = tool.tool_call_schema
    # 2. schema 不是 Pydantic 模型时无法做字段级校验，原样透传指令参数。
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        return SlotAssessment(arguments=directive.arguments)
    # 3. 未提供参数时，枚举 schema 中的必填字段作为缺失槽位。
    if directive.arguments is None:
        required = tuple(name for name, field in schema.model_fields.items() if field.is_required())
        return SlotAssessment(arguments=None, missing_fields=required)
    # 4. 校验参数：缺失字段与字段级错误分别归集后返回。
    try:
        validated = schema.model_validate(directive.arguments)
    except ValidationError as exc:
        missing: list[str] = []
        errors: list[str] = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            if error["type"] == "missing":
                missing.append(location)
            else:
                errors.append(f"{location}: {error['msg']}")
        return SlotAssessment(
            arguments=None,
            missing_fields=tuple(dict.fromkeys(missing)),
            validation_errors=tuple(errors),
        )
    # 5. 校验通过，返回 JSON 化的最终参数字典。
    return SlotAssessment(arguments=validated.model_dump(mode="json"))
