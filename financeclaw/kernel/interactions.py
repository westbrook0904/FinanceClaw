"""有界的资料、选择、审批契约；Schema 由发布代码声明，不由模型定义。"""

import json
from typing import Any, Literal, Self

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class InteractionPoint(BaseModel):
    """Agent 发布时固定的交互点，问题正文可变化但权限、类型与回答结构不可变化。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # point_id 标识发布声明，可产生多个运行期实例；它不是 interaction_id。
    point_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: Literal["input", "choice", "approval"]
    question: str = Field(min_length=1, max_length=2000)
    # input 使用自包含的对象 Schema，choice 使用精确 options；approval
    # 的动作快照在运行期登记，并由 required_scope 约束回答者权限。
    response_schema: dict[str, Any] = Field(default_factory=dict)
    options: tuple[str, ...] = Field(default=(), max_length=20)
    selection_mode: Literal["single", "multiple"] = "single"
    min_selected: int = Field(default=1, ge=0, le=20)
    max_selected: int = Field(default=20, ge=1, le=20)
    required_scope: str | None = Field(default=None, min_length=1, max_length=128)
    # 有效期从首次登记计算，重复观察不能给同一中断续期。
    timeout_seconds: int = Field(default=900, ge=1, le=604800)

    @model_validator(mode="after")
    def validate_declaration(self) -> Self:
        """禁止外部 Schema 引用，校验成本有界；选择只能回答已发布的精确选项。"""
        encoded = json.dumps(self.response_schema)
        if len(encoded.encode()) > 16384:
            raise ValueError("interaction response schema is too large")

        def check(value: Any) -> None:
            """递归拒绝所有引用，包括隐含网络解析和动态引用。"""
            if isinstance(value, dict):
                if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef", "$id")):
                    raise ValueError(
                        "interaction schemas must be self-contained without references"
                    )
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.response_schema)
        Draft202012Validator.check_schema(self.response_schema)
        if self.kind == "input" and self.response_schema.get("type") != "object":
            raise ValueError("input interaction requires a declared object schema")
        if self.kind == "choice" and (
            not self.options
            or len(set(self.options)) != len(self.options)
            or any(not option or len(option) > 256 for option in self.options)
        ):
            raise ValueError("choice requires bounded unique options")
        if self.kind != "choice" and self.options:
            raise ValueError("only choice interactions declare options")
        if self.selection_mode == "multiple" and (
            self.kind != "choice" or self.min_selected > min(self.max_selected, len(self.options))
        ):
            raise ValueError("multiple choice requires valid selection bounds")
        if self.kind == "approval" and not self.required_scope:
            raise ValueError("declared approval requires an explicit approval scope")
        return self

    def normalize_answer(self, answer: Any) -> Any:
        """双端共用回答校验，多选按发布顺序规范化以稳定幂等摘要。"""
        if self.kind == "input":
            Draft202012Validator(self.response_schema).validate(answer)
            return answer
        if self.kind != "choice":
            raise ValueError("approval has no answer")
        if self.selection_mode == "single":
            if not isinstance(answer, str) or answer not in self.options:
                raise ValueError("response is not a published choice")
            return answer
        if (
            not isinstance(answer, list)
            or not all(isinstance(value, str) for value in answer)
            or len(set(answer)) != len(answer)
            or not self.min_selected <= len(answer) <= self.max_selected
            or any(value not in self.options for value in answer)
        ):
            raise ValueError("response is not a valid multiple choice")
        return [value for value in self.options if value in answer]


class InteractionResponse(BaseModel):
    """客户端必须回传实例版本；回答不是授权，审批必须绑定完整动作摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 必须回传所见问题的 revision；服务端会同时比对当前 owner 的执行尝试。
    revision: int = Field(ge=1)
    kind: Literal["input", "choice", "approval"]
    answer: Any = None
    # approval 只接受 decision + action_hash；input/choice 只接受 answer。
    decision: Literal["approve", "reject"] | None = None
    action_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        """分型避免把任意文本转换成 approve，也不接受原地改写审批动作。"""
        if self.kind == "approval":
            if self.decision is None or self.action_hash is None or self.answer is not None:
                raise ValueError("approval requires decision and action_hash, not answer")
        elif self.answer is None or self.decision is not None or self.action_hash is not None:
            raise ValueError("input/choice requires answer, not an approval decision")
        if len(json.dumps(self.answer, ensure_ascii=False, allow_nan=False).encode()) > 16384:
            raise ValueError("interaction answer exceeds 16 KiB")
        return self
