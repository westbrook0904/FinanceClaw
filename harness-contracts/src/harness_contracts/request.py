"""调用方提交给 Harness 的通用请求协议。"""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field

from .base import ContractModel, FrozenJsonMapping, FrozenJsonValue, NonEmptyString


class RequestInput(ContractModel):
    """与业务无关的输入载荷。"""

    type: NonEmptyString
    content: FrozenJsonValue


class RequestOptions(ContractModel):
    """单次 Invocation 的通用执行选项。"""

    timeout_ms: int | None = Field(default=None, gt=0)
    trace: bool = True


class Request(ContractModel):
    """Harness 的标准请求入口。

    ``target`` 为空时由未来的顶层 Agent 解释；当前 Direct Invocation 入口仍要求显式目标。
    """

    request_id: NonEmptyString = Field(default_factory=lambda: uuid4().hex)
    session_id: NonEmptyString | None = None
    tenant_id: NonEmptyString | None = None
    user_id: NonEmptyString | None = None
    input: RequestInput
    metadata: FrozenJsonMapping = Field(default_factory=dict)
    options: RequestOptions = Field(default_factory=RequestOptions)
