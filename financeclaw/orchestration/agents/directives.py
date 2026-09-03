"""解析显式 Agent、工作流或工具调用指令并校验工具参数槽位。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

_DIRECTIVE = re.compile(
    r"^/(?P<kind>tool|workflow|agent)\s+"
    r"(?P<resource>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})"
    r"(?:\s+(?P<payload>.*))?$",
    re.DOTALL,
)


class InvocationKind(StrEnum):
    """显式调用指令指向工具、工作流或 Agent。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        TOOL: 显式调用目标是一个受治理工具。
        WORKFLOW: 显式调用或委派目标是确定性工作流。
        AGENT: 显式调用或委派目标是专业 Agent。
    """

    TOOL = "tool"
    WORKFLOW = "workflow"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class InvocationDirective:
    """定义调用Directive。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        kind: 记录或目标的语义类别。
        resource_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        payload: 事件携带的结构化业务数据。
        arguments: 传给目标工具或工作流的已解析参数。
        parse_error: 显式指令无法解析时的原因；解析成功时为空。
    """

    kind: InvocationKind
    resource_id: str
    payload: str | None = None
    arguments: dict[str, Any] | None = None
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class SlotAssessment:
    """定义参数槽位Assessment。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        arguments: 传给目标工具或工作流的已解析参数。
        missing_fields: 工具入参仍缺少的必填字段名称。
        validation_errors: 工具入参模型返回的字段级校验错误。
    """

    arguments: dict[str, Any] | None
    missing_fields: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """仅当参数已解析、没有缺失字段且没有校验错误时返回真。"""
        return self.arguments is not None and not self.validation_errors


def parse_invocation_directive(message: str) -> InvocationDirective | None:
    """解析外部表示并转换为directives 模块的数据。"""
    match = _DIRECTIVE.fullmatch(message.strip())
    if match is None:
        return None
    payload = match.group("payload")
    payload = payload.strip() if payload is not None else None
    arguments: dict[str, Any] | None = None
    parse_error: str | None = None
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
    """合并显式指令参数与工具 schema，返回缺失字段和校验错误。"""
    if directive.parse_error is not None:
        return SlotAssessment(arguments=None, validation_errors=(directive.parse_error,))
    schema = tool.tool_call_schema
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        return SlotAssessment(arguments=directive.arguments)
    if directive.arguments is None:
        required = tuple(name for name, field in schema.model_fields.items() if field.is_required())
        return SlotAssessment(arguments=None, missing_fields=required)
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
    return SlotAssessment(arguments=validated.model_dump(mode="json"))
