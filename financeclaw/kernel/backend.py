"""Minimal native attempt and notification identities without child-delivery contracts."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BackendModel(BaseModel):
    """Keep the frozen strict wire shape of existing attempt/notification records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BackendExecutionRef(BackendModel):
    """一个业务 run 的一次 backend 尝试；execution_id 仅由对应 Adapter 解释。"""

    backend_instance_id: Identifier
    task_id: Identifier
    operation_id: Identifier
    execution_id: Annotated[str, Field(min_length=1, max_length=2048)]


class BackendNotification(BackendModel):
    """已认证通知的最小线索；没有 tenant、授权、输入、结果或完成决定。"""

    schema_version: Literal[1] = 1
    backend_instance_id: Identifier
    execution_id: Annotated[str, Field(min_length=1, max_length=2048)]
    event_id: Identifier | None = None
    status_hint: Annotated[str, Field(min_length=1, max_length=32)]
    payload_digest: Digest
    received_at: AwareDatetime
