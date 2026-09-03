"""跨层共享的运行时上下文契约：租户、主体、会话定位与数据分级。

本模块属于 kernel（稳定共享契约层），被 orchestration 与 infrastructure 依赖，
自身不依赖任何业务模块，变更需保持向后兼容。
"""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DataClassification(StrEnum):
    """数据敏感级别枚举，标注一次运行所涉数据的最高密级。

    使用场景：构造 ``ExecutionContext`` 时声明密级；审计、记忆与观测等
    下游组件依据密级决定脱敏、留存与展示策略。
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# 通用标识符类型：1~128 位，仅允许字母、数字与 . _ : -，用于各类 ID 字段的统一校验。
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class ExecutionContext(BaseModel):
    """一次运行的执行上下文：携带租户、主体、会话定位与数据分级等背景信息。

    使用场景：BFF 受理请求后构造，随 Run 贯穿 orchestration 与 infrastructure，
    用于多租户隔离、授权判定、审计归属与观测标注。

    Attributes:
        tenant_id: 租户 ID，多租户隔离与存储命名空间的一级维度。
        subject_id: 主体 ID（通常是终端用户），权限校验与审计记录的归属对象。
        scopes: 授予本次运行的作用域集合，供 Tool/Workflow 的授权策略校验。
        conversation_id: 会话 ID；不经会话的直连运行（裸 Tool/Workflow）可为 None。
        turn_id: 轮次 ID，定位会话中由一次用户输入触发的工作单元。
        run_id: 本次运行的唯一 ID，贯穿审计、状态查询与流式事件。
        data_classification: 本次运行的数据密级，默认 ``INTERNAL``。
        locale: 语言环境标签，影响回复语言与本地化格式，默认 ``zh-CN``。
        timezone: IANA 时区名，用于时间展示与调度类逻辑，默认 ``Asia/Shanghai``。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: Identifier
    subject_id: Identifier
    scopes: frozenset[str] = Field(default_factory=frozenset)
    conversation_id: Identifier | None = None
    turn_id: Identifier
    run_id: Identifier
    data_classification: DataClassification = DataClassification.INTERNAL
    locale: Annotated[str, Field(min_length=2, max_length=32)] = "zh-CN"
    timezone: Annotated[str, Field(min_length=1, max_length=64)] = "Asia/Shanghai"

    def trace_metadata(self) -> dict[str, str]:
        """生成供分布式追踪使用的元数据字典，租户与主体 ID 经哈希脱敏。

        Returns:
            包含租户/主体哈希、轮次与运行 ID、数据密级的追踪标签字典。

        """
        from hashlib import sha256

        def digest(value: str) -> str:
            """对标识符取 SHA-256 摘要，仅保留前 16 位十六进制用于脱敏。"""
            # 仅保留摘要前 16 位十六进制，避免完整哈希被反推或滥用。
            return sha256(value.encode()).hexdigest()[:16]

        return {
            "tenant_hash": digest(self.tenant_id),
            "subject_hash": digest(self.subject_id),
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "data_classification": self.data_classification.value,
        }
