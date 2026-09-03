"""定义大结果外置后使用的不可变制品引用。"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactMetadata(BaseModel):
    """定义制品Metadata。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        artifact_id: 制品稳定标识。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        content_type: 制品内容的 MIME 类型，供下载方选择解析方式。
        storage_uri: 制品内容的存储位置，不包含访问凭证。
        content_hash: 正文的 SHA-256，用于完整性校验、去重与审计。
        size_bytes: 制品序列化后的字节数。
        source_type: 内容来源类别，例如用户陈述或系统推导。
        source_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        access_policy: 读取制品所需满足的租户、主体或权限限制。
        encryption_metadata: 证明制品静态加密方式的非敏感元数据。
        created_at: 记录创建时间，统一按 UTC 解释。
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
