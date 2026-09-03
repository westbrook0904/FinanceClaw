"""定义长期记忆、候选、检索结果和审计证据模型。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenMemoryModel(BaseModel):
    """定义不可变记忆模型。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryType(StrEnum):
    """可长期保存的信息语义类别。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        PREFERENCE: 表示 `preference` 这一受限枚举值。
        GOAL: 表示 `goal` 这一受限枚举值。
        CONSTRAINT: 表示 `constraint` 这一受限枚举值。
        DECISION_NOTE: 表示 `decision_note` 这一受限枚举值。
    """

    PREFERENCE = "preference"
    GOAL = "goal"
    CONSTRAINT = "constraint"
    DECISION_NOTE = "decision_note"


class MemoryStatus(StrEnum):
    """长期记忆当前是否可检索或已撤销。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        ACTIVE: 记录当前有效，可继续读取或追加操作。
        SUPERSEDED: 该版本已被新版本替代，不再作为当前有效记录。
        REVOKED: 表示 `revoked` 这一受限枚举值。
        DELETED: 表示 `deleted` 这一受限枚举值。
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    DELETED = "deleted"


class MemorySensitivity(StrEnum):
    """定义记忆Sensitivity。

    适用场景：
        用于限制持久化值和边界输入，避免以自由字符串表达状态。

    属性：
        PUBLIC: 无需访问控制即可公开的数据等级。
        INTERNAL: 仅允许访问平台内部网络资源或处理内部级数据。
        CONFIDENTIAL: 需要严格访问控制的机密数据等级。
        RESTRICTED: 受最严格限制、通常不得发送给外部供应方的数据等级。
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


MemoryIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class MemoryDraft(FrozenMemoryModel):
    """定义记忆Draft。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        kind: 记录或目标的语义类别。
        content: 经过边界校验后保存或传递的正文内容。
        evidence_message_ids: 关联对象标识的有序集合。
    """

    kind: MemoryType
    content: Annotated[str, Field(min_length=1, max_length=2_000)]
    evidence_message_ids: tuple[MemoryIdentifier, ...] = Field(min_length=1, max_length=32)

    @field_validator("content")
    @classmethod
    def content_must_be_trimmed(cls, value: str) -> str:
        """校验记忆Draft的跨字段一致性；不满足不变量时拒绝构造。"""
        if value != value.strip():
            raise ValueError("memory content must not contain surrounding whitespace")
        return value

    @field_validator("evidence_message_ids")
    @classmethod
    def evidence_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """校验记忆Draft的跨字段一致性；不满足不变量时拒绝构造。"""
        if len(value) != len(set(value)):
            raise ValueError("memory evidence message IDs must be unique")
        return value


class MemoryProvenance(FrozenMemoryModel):
    """定义记忆Provenance。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        producer: 产生该记忆内容的用户、Agent 或系统组件标识。
    """

    conversation_id: MemoryIdentifier
    turn_id: MemoryIdentifier
    run_id: MemoryIdentifier
    producer: str = "financeclaw.long_term_memory_service"


class MemoryProposal(FrozenMemoryModel):
    """定义尚待策略或用户确认的长期记忆候选。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        proposal_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        draft: 尚未提交为长期记忆的候选事实。
        sensitivity: 允许处理的数据敏感级别。
        requires_confirmation: 该候选是否必须经用户确认后才能成为有效长期记忆。
        confirmation_reason: 要求确认或允许自动提交的策略理由。
        policy_version: 作出决策时使用的策略版本。
    """

    proposal_id: MemoryIdentifier
    draft: MemoryDraft
    sensitivity: MemorySensitivity
    requires_confirmation: bool
    confirmation_reason: str
    policy_version: str


class MemoryRecord(FrozenMemoryModel):
    """定义记忆的持久化记录。

    适用场景：
        用于跨步骤保存不可变事实，并支持持久化或审计重放。

    属性：
        memory_id: 长期记忆稳定标识。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        namespace: LangGraph Store 中用于隔离租户、主体和记忆类别的路径。
        memory_type: 长期记忆的语义类别。
        content: 经过边界校验后保存或传递的正文内容。
        status: 当前生命周期状态，决定记录允许的后续操作。
        source_message_ids: 生成摘要时使用的原始消息标识，保留证据链。
        created_at: 记录创建时间，统一按 UTC 解释。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
        supersedes_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        sensitivity: 允许处理的数据敏感级别。
        provenance: 内容来源、生成方式和版本组成的可审计来源信息。
        valid_until: 该事实可被使用的截止时间；为空表示没有显式有效期。
        schema_version: 记录结构版本，用于兼容演进和历史数据解析。
    """

    memory_id: MemoryIdentifier
    tenant_id: MemoryIdentifier
    subject_id: MemoryIdentifier
    namespace: tuple[str, ...] = Field(min_length=5, max_length=5)
    memory_type: MemoryType
    content: Annotated[str, Field(min_length=1, max_length=2_000)]
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_message_ids: tuple[MemoryIdentifier, ...] = Field(min_length=1, max_length=32)
    created_at: datetime
    updated_at: datetime
    supersedes_id: MemoryIdentifier | None = None
    sensitivity: MemorySensitivity
    provenance: MemoryProvenance
    valid_until: datetime | None = None
    schema_version: int = Field(default=1, ge=1)

    @field_validator("created_at", "updated_at", "valid_until")
    @classmethod
    def timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        """校验记忆的持久化记录的跨字段一致性；不满足不变量时拒绝构造。"""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("memory timestamps must include timezone information")
        return value

    @model_validator(mode="after")
    def validate_lifecycle_fields(self) -> Self:
        """校验记忆的持久化记录的跨字段一致性；不满足不变量时拒绝构造。"""
        if self.updated_at < self.created_at:
            raise ValueError("memory updated_at cannot precede created_at")
        if self.valid_until is not None and self.valid_until <= self.created_at:
            raise ValueError("memory valid_until must be after created_at")
        if self.supersedes_id == self.memory_id:
            raise ValueError("memory cannot supersede itself")
        return self


class MemoryRecall(FrozenMemoryModel):
    """定义带相关性与注入理由的长期记忆检索结果。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        record: 检索命中的完整长期记忆记录。
        reason: 产生当前决策、遗漏或状态的可读原因。
        score: 评测得分，通常归一化到 0 至 1。
    """

    record: MemoryRecord
    reason: str
    score: float = Field(ge=0)
