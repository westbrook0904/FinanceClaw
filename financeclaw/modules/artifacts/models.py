"""Artifact 元数据的领域模型定义。

位于 artifacts 模块的模型层，用 Pydantic 描述被 offload 到外部存储的工具结果工件，
字段与 conversation 模块的 ``ArtifactMetadataRow`` 持久化结构一一对应。
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactMetadata(BaseModel):
    """一条被 offload 到 Artifact Store 的工件元数据，本身不携带工件内容。

    使用场景：工具结果超过内联阈值时，由 ArtifactService 在写入存储并落库时构造；
    读取时作为归属校验（tenant/subject）与完整性校验（SHA256）的依据。

    Attributes:
        artifact_id: 工件唯一标识，形如 ``artifact-<uuid4 hex>``，同时作为存储与数据库主键。
        tenant_id: 租户标识，用于多租户数据隔离。
        subject_id: 主体标识，与 tenant_id 共同限定该工件的读取归属。
        content_type: 工件内容的 MIME 类型，通常为 ``application/json``。
        storage_uri: 存储后端返回的存储地址，如 ``artifact-s3://bucket/key``。
        content_hash: 工件内容的 SHA256 摘要，限定为 64 位小写十六进制，读取时用于完整性校验。
        size_bytes: 工件内容字节数，约束为非负整数。
        source_type: 产生该工件的来源类型（如工具、工作流），用于溯源。
        source_id: 产生该工件的来源对象标识，与 source_type 共同定位来源。
        access_policy: 访问策略描述，如要求调用方具备 ``artifacts:read`` 权限范围。
        encryption_metadata: 存储后端的服务端加密描述，如 SSE 算法与 KMS 密钥标识。
        created_at: 元数据创建时间（UTC 带时区），默认为构造时的当前时间。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    tenant_id: str
    subject_id: str
    content_type: str
    storage_uri: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    source_type: str
    source_id: str
    access_policy: dict[str, Any] = Field(default_factory=dict)
    encryption_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
