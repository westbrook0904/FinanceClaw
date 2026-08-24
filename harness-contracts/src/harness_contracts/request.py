"""调用方提交给 Harness 的通用请求协议。"""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, FrozenJsonValue, NonEmptyString


class RequestInput(ContractModel):
    """与业务无关的输入载荷。"""

    type: NonEmptyString
    content: FrozenJsonValue


class RequestTarget(ContractModel):
    """阶段一的显式调用目标。

    `capability` 必填，因此 Runtime 暂时不需要 Planner 或 LLM Router。
    `plugin` 仅作为可选的 Provider 限定条件，能力名称仍是主要路由语义。
    """

    capability: NonEmptyString
    plugin: NonEmptyString | None = None


class RequestOptions(ContractModel):
    """单次 Invocation 的通用执行选项。"""

    timeout_ms: int | None = Field(default=None, gt=0)
    trace: bool = True


class Request(ContractModel):
    """Harness 的标准请求入口。"""

    request_id: NonEmptyString = Field(default_factory=lambda: uuid4().hex)
    session_id: NonEmptyString | None = None
    tenant_id: NonEmptyString | None = None
    user_id: NonEmptyString | None = None
    input: RequestInput
    metadata: FrozenJsonMapping = Field(default_factory=dict)
    target: RequestTarget
    options: RequestOptions = Field(default_factory=RequestOptions)
