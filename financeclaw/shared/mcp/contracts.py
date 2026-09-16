"""MCP 运行时只比较输入参数结构，说明性文案仍随完整发布定义保存。"""

import json

INPUT_CONTRACT_VERSION = "input-structure/1"
_ANNOTATIONS = {
    "title",
    "description",
    "$comment",
    "examples",
    "default",
    "deprecated",
    "readOnly",
    "writeOnly",
}
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMAS = {
    "items",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}
_SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _ordered(values):
    """仅对集合语义的关键字排序，不改变元组位置或业务常量内容。"""
    return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))


def input_structure(schema):
    """按 Schema 位置剔除注释，保留同名参数以及 enum/const 内的业务数据。"""
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in _ANNOTATIONS:
            continue
        if key in _SCHEMA_MAPS:
            value = {name: input_structure(child) for name, child in value.items()}
        elif key in _SCHEMAS:
            value = (
                [input_structure(child) for child in value]
                if isinstance(value, list)
                else input_structure(value)
            )
        elif key in _SCHEMA_LISTS:
            value = [input_structure(child) for child in value]
            if key != "prefixItems":
                value = _ordered(value)
        elif key in {"required", "enum", "type"} and isinstance(value, list):
            value = _ordered(value)
        elif key in {"dependencies", "dependentRequired"}:
            value = {
                name: _ordered(child) if isinstance(child, list) else input_structure(child)
                for name, child in value.items()
            }
        result[key] = value
    return result
