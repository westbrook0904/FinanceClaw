"""共享 Provider Fabric 上的模型生成协议、Provider SPI 与 Gateway。"""

from .contracts import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
    ModelFinishReason,
    ModelMessage,
    ModelOutput,
    ModelResponseFormat,
    ModelRole,
    ModelUsage,
)
from .gateway import ModelGateway
from .mocks import (
    DEFAULT_MODEL_CAPABILITY_ID,
    MockBackupModel,
    MockFastModel,
    MockModelProvider,
    MockQualityModel,
)
from .provider import ModelProvider

__all__ = [
    "DEFAULT_MODEL_CAPABILITY_ID",
    "GenerateRequest",
    "GenerateResult",
    "GenerateStatus",
    "ModelFinishReason",
    "ModelGateway",
    "ModelMessage",
    "ModelOutput",
    "ModelProvider",
    "ModelResponseFormat",
    "ModelRole",
    "ModelUsage",
    "MockBackupModel",
    "MockFastModel",
    "MockModelProvider",
    "MockQualityModel",
]
