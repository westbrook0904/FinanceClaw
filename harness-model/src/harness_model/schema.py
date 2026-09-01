"""有资源上限、无远程解析的本地 JSON Schema 边界。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from harness_contracts import StructuredOutputSpec
from jsonschema import Draft202012Validator, FormatChecker, SchemaError, ValidationError


@dataclass(frozen=True, slots=True)
class SchemaValidationFailure(ValueError):
    kind: str
    validator: str | None = None
    path: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"JSON Schema validation failed: {self.kind}"


def structured_schema_hash(spec: StructuredOutputSpec) -> str:
    if not isinstance(spec, StructuredOutputSpec):
        raise TypeError("spec must be StructuredOutputSpec")
    return _stable_hash(
        {
            "name": spec.name,
            "schema": spec.model_dump(mode="json")["schema"],
            "strictness": spec.strictness.value,
            "on_unsupported": spec.on_unsupported.value,
        }
    )


def validate_schema_definition(spec: StructuredOutputSpec) -> Draft202012Validator:
    """校验 Schema 定义并返回不会远程解析的本地 Validator。"""

    if not isinstance(spec, StructuredOutputSpec):
        raise TypeError("spec must be StructuredOutputSpec")
    schema = spec.model_dump(mode="json")["schema"]
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except SchemaError as exc:
        raise SchemaValidationFailure(
            "invalid_schema",
            validator=(str(exc.validator)[:80] if exc.validator is not None else None),
            path=tuple(str(part)[:80] for part in list(exc.path)[:16]),
        ) from exc


def validate_structured_value(
    validator: Draft202012Validator,
    value: object,
) -> None:
    """Validate JSON data, including Harness immutable JSON containers.

    Contract models freeze JSON objects as ``MappingProxyType`` and arrays as
    tuples. ``jsonschema`` intentionally recognizes only ``dict`` and ``list``
    for the corresponding JSON types, so validation must use a transient plain
    JSON view without changing the immutable value returned to callers.
    """

    if not isinstance(validator, Draft202012Validator):
        raise TypeError("validator must be Draft202012Validator")
    try:
        validator.validate(_plain_json_containers(value))
    except ValidationError as exc:
        raise SchemaValidationFailure(
            "invalid_output",
            validator=(str(exc.validator)[:80] if exc.validator is not None else None),
            path=tuple(str(part)[:80] for part in list(exc.absolute_path)[:16]),
        ) from exc


def _plain_json_containers(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _plain_json_containers(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_plain_json_containers(item) for item in value]
    return value


def stable_request_fingerprint(value: object) -> str:
    return _stable_hash(value)


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
