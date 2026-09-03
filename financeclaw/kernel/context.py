"""跨层共享且不依赖具体实现的运行上下文、目标与响应契约。"""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DataClassification(StrEnum):
    """运行上下文或供应商配置允许处理的数据敏感等级。

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


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class ExecutionContext(BaseModel):
    """定义跨 Agent、图与工具传递的可信调用上下文。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        scopes: 调用主体拥有的权限域集合。
        conversation_id: 会话稳定标识，用于关联消息、轮次、摘要和上下文清单。
        turn_id: 会话轮次标识，用于把一次用户输入与其运行结果关联。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        data_classification: 本次运行处理的数据分类，用于约束模型和工具选择。
        locale: 模型面向用户生成内容时采用的语言与地区标记。
        timezone: 解释用户时间和展示时间时采用的 IANA 时区。
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
        """生成不含原始租户和主体标识的追踪元数据；敏感标识仅输出截断哈希。"""
        from hashlib import sha256

        def digest(value: str) -> str:
            """计算不可逆的截断 SHA-256，避免在追踪标签中暴露原始标识。"""
            return sha256(value.encode()).hexdigest()[:16]

        return {
            "tenant_hash": digest(self.tenant_id),
            "subject_hash": digest(self.subject_id),
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "data_classification": self.data_classification.value,
        }
