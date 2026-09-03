"""Parse user invocation hints without promoting them to trusted targets."""

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
    TOOL = "tool"
    WORKFLOW = "workflow"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class InvocationDirective:
    """A parsed, explicitly user-controlled invocation preference."""

    kind: InvocationKind
    resource_id: str
    payload: str | None = None
    arguments: dict[str, Any] | None = None
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class SlotAssessment:
    """Schema validation result used to choose between execution and elicitation."""

    arguments: dict[str, Any] | None
    missing_fields: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.arguments is not None and not self.validation_errors


def parse_invocation_directive(message: str) -> InvocationDirective | None:
    """Parse the supported slash form only when it starts the user message.

    Free text after the resource identifier remains available to the Agent for
    semantic extraction.  An object-shaped payload is parsed eagerly so a
    complete tool call can be validated and executed without a clarification turn.
    """

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
    """Validate explicit JSON arguments or report the required empty slots."""

    if directive.parse_error is not None:
        return SlotAssessment(arguments=None, validation_errors=(directive.parse_error,))
    # ``tool_call_schema`` excludes trusted ToolRuntime/Store/State injections.
    # Slot filling must reason only about fields controlled by the user/model.
    schema = tool.tool_call_schema
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        # A tool without a Pydantic schema cannot participate in deterministic
        # slot validation; leave natural-language extraction to the Agent.
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
