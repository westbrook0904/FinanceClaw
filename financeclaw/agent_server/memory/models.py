"""长期记忆的领域模型：记忆类别、生命周期、敏感级别与草案、提案、记录、召回结构。

模型全部为不可变 Pydantic 模型，在接口、领域与持久化边界间传递经过校验的数据。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenMemoryModel(BaseModel):
    """所有长期记忆模型共用的不可变基类。

    使用场景：
        作为记忆草案、提案、记录与召回结果的基类，保证结构化数据在
        模块之间传递时不被意外篡改或扩展。

    Attributes:
        model_config: Pydantic 配置；禁止未知字段（extra="forbid"）并冻结实例。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryType(StrEnum):
    """长期记忆保存的信息语义类别，共四类。

    使用场景：
        写入提案时声明记忆用途，检索时按类别过滤召回范围。

    Attributes:
        PREFERENCE: 用户表达的稳定偏好，如沟通风格与呈现方式。
        GOAL: 用户的阶段性目标，用于跨会话对齐任务方向。
        CONSTRAINT: 必须遵守的硬性约束，召回时无条件进入模型上下文。
        DECISION_NOTE: 已作出的金融决策及其理由备注。

    """

    PREFERENCE = "preference"
    GOAL = "goal"
    CONSTRAINT = "constraint"
    DECISION_NOTE = "decision_note"


class MemoryStatus(StrEnum):
    """长期记忆记录的生命周期状态。

    使用场景：
        Store 检索只保留 ACTIVE 记录；supersede/revoke/delete 驱动状态迁移。

    Attributes:
        ACTIVE: 当前有效，可被召回并参与语义索引。
        SUPERSEDED: 已被新版本取代，保留历史但不再召回。
        REVOKED: 已被主动撤销，不再召回。
        DELETED: 已被删除，仅保留审计痕迹。

    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    DELETED = "deleted"


class MemorySensitivity(StrEnum):
    """记忆内容允许处理的数据敏感级别。

    使用场景：
        决定记忆可进入的提示区域与审计元数据；高敏感内容不得进入低级别上下文。

    Attributes:
        PUBLIC: 可公开处理的数据级别。
        INTERNAL: 仅限平台内部处理的默认级别。
        CONFIDENTIAL: 涉及高影响金融画像的机密级别，写入需显式确认。
        RESTRICTED: 受最严格限制、默认不进入模型上下文的级别。

    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# 记忆相关稳定标识的类型别名：非空、不超过 128 字符，仅允许字母数字与 `._:-`。
MemoryIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class MemoryDraft(FrozenMemoryModel):
    """待写入的长期记忆草案，尚无租户归属与生命周期信息。

    使用场景：
        由记忆流程从对话证据中提炼，经策略评估后进入提案与确认流程。

    Attributes:
        kind: 记忆的语义类别，见 MemoryType。
        content: 记忆正文，1 到 2000 字符，不允许首尾空白。
        evidence_message_ids: 支撑该记忆的会话消息标识，1 到 32 个且不重复；
            可包含占位符 `current`，由服务解析为当前轮次的用户消息。

    """

    kind: MemoryType
    content: Annotated[str, Field(min_length=1, max_length=2_000)]
    evidence_message_ids: tuple[MemoryIdentifier, ...] = Field(min_length=1, max_length=32)

    @field_validator("content")
    @classmethod
    def content_must_be_trimmed(cls, value: str) -> str:
        """拒绝包含首尾空白的记忆正文，保证写入内容已规范化。"""
        if value != value.strip():
            raise ValueError("memory content must not contain surrounding whitespace")
        return value

    @field_validator("evidence_message_ids")
    @classmethod
    def evidence_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝重复的证据消息标识，保证证据链没有冗余引用。"""
        if len(value) != len(set(value)):
            raise ValueError("memory evidence message IDs must be unique")
        return value


class MemoryProvenance(FrozenMemoryModel):
    """长期记忆的可审计来源信息。

    使用场景：
        记录记忆产生时的会话、轮次与运行标识，支撑审计回放与来源追溯。

    Attributes:
        conversation_id: 产生该记忆的会话标识。
        turn_id: 产生该记忆的会话轮次标识。
        run_id: 产生该记忆的应用侧运行标识。
        producer: 产生该记忆的服务组件标识，默认为长期记忆服务自身。

    """

    conversation_id: MemoryIdentifier
    turn_id: MemoryIdentifier
    run_id: MemoryIdentifier
    producer: str = "financeclaw.long_term_memory_service"


class MemoryProposal(FrozenMemoryModel):
    """策略评估后输出的记忆写入提案，等待 HITL 人工确认。

    使用场景：
        propose 阶段产出并写入审计；confirm 阶段凭 proposal_id 复验后落库。

    Attributes:
        proposal_id: 由租户、主体、草案与策略版本共同决定的确定性标识，
            防止提案与确认之间的事实被替换。
        draft: 待写入的记忆草案，证据引用已解析完成。
        sensitivity: 策略判定的数据敏感级别。
        requires_confirmation: 是否必须经用户显式确认后才能写入。
        confirmation_reason: 要求确认或允许自动提交的策略理由。
        policy_version: 作出评估时使用的治理策略版本。

    """

    proposal_id: MemoryIdentifier
    draft: MemoryDraft
    sensitivity: MemorySensitivity
    requires_confirmation: bool
    confirmation_reason: str
    policy_version: str


class MemoryRecord(FrozenMemoryModel):
    """已写入 LangGraph Store 的长期记忆持久化记录。

    使用场景：
        作为 Store 中记忆条目的规范形态，跨会话召回、生命周期管理与审计
        都围绕它进行；Store 原始值必须能投影回本模型才视为合法。

    Attributes:
        memory_id: 由 proposal_id 派生的确定性记忆标识。
        tenant_id: 租户隔离键，所有读写都必须落在该租户命名空间内。
        subject_id: 已认证主体标识，用于所有权校验与审计归因。
        namespace: LangGraph Store 命名空间路径，固定 5 段：
            根路径 3 段，加 URL 安全转义后的租户、主体标签各 1 段。
        memory_type: 记忆的语义类别。
        content: 记忆正文，1 到 2000 字符。
        status: 生命周期状态，默认 ACTIVE。
        source_message_ids: 支撑该记忆的原始消息标识，保留证据链。
        created_at: 创建时间，必须携带时区信息。
        updated_at: 最近一次状态变更时间，不得早于 created_at。
        supersedes_id: 被本记录取代的旧记忆标识；无取代关系时为空。
        sensitivity: 数据敏感级别。
        provenance: 记忆来源的会话、轮次与运行信息。
        valid_until: 记忆的有效截止时间；为空表示长期有效。
        schema_version: 记录结构版本号，从 1 起递增以支持演进。

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
        """拒绝缺少时区信息的时间字段，保证跨时区的时间比较可靠。"""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("memory timestamps must include timezone information")
        return value

    @model_validator(mode="after")
    def validate_lifecycle_fields(self) -> Self:
        """校验生命周期不变量：更新不早于创建、有效期晚于创建且记忆不可自取代。"""
        if self.updated_at < self.created_at:
            raise ValueError("memory updated_at cannot precede created_at")
        if self.valid_until is not None and self.valid_until <= self.created_at:
            raise ValueError("memory valid_until must be after created_at")
        if self.supersedes_id == self.memory_id:
            raise ValueError("memory cannot supersede itself")
        return self


class MemoryRecall(FrozenMemoryModel):
    """单条记忆的召回结果，附带命中理由与相关性得分。

    使用场景：
        search 的返回单元；调用方据此筛选注入模型上下文的记忆并解释命中原因。

    Attributes:
        record: 命中的记忆记录。
        reason: 命中理由，如活跃约束、活跃目标、语义或词法相关。
        score: 相关性得分，取语义与词法得分的较大值，不小于 0。

    """

    record: MemoryRecord
    reason: str
    score: float = Field(ge=0)
