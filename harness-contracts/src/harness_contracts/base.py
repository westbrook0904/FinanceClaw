"""Contracts 模块共享的基础类型和模型配置。"""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]


def _freeze_json(value: JsonValue) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> JsonValue:
    if isinstance(value, dict | MappingProxyType):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_json(item) for item in value]
    return value


FrozenJsonValue = Annotated[
    JsonValue,
    AfterValidator(_freeze_json),
    PlainSerializer(_thaw_json, return_type=JsonValue),
]
FrozenJsonMapping = Annotated[
    dict[str, JsonValue],
    AfterValidator(_freeze_json),
    PlainSerializer(_thaw_json, return_type=dict[str, JsonValue]),
]

NonEmptyString = Annotated[str, Field(min_length=1)]


class ContractModel(BaseModel):
    """所有稳定契约的基础模型。

    未声明字段会被拒绝，以便尽早暴露协议版本或字段拼写错误。模型默认冻结，
    从而使 Request、Context、Descriptor 和 Result 可安全地跨模块传递。
    """
    # extra="forbid"：传入未声明字段直接报错。
    # frozen=True：实例创建后字段只读。
    # str_strip_whitespace=True：字符串输入自动去掉首尾空白。
    # validate_default=True：默认值也经过 Pydantic 校验。
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class MutableContractModel(ContractModel):
    """仅供明确需要在一次 Invocation 中变化的状态模型使用。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )
