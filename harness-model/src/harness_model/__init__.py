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
    MockStrictModelProvider,
)
from .openai import (
    OPENAI_RESPONSES_MODEL_CAPABILITY_ID,
    OPENAI_RESPONSES_PROVIDER_ID,
    HttpxJsonTransport,
    JsonHttpResponse,
    JsonHttpTransport,
    OpenAIResponsesModelProvider,
)
from .preparation import (
    ModelAttemptPolicy,
    ModelGenerationCheckpointSink,
    PreparedModelGeneration,
    PreparedStructuredOutput,
)
from .provider import ModelProvider
from .structured import StructuredGenerationAdapter

__all__ = [
    "DEFAULT_MODEL_CAPABILITY_ID",
    "GenerateRequest",
    "GenerateResult",
    "GenerateStatus",
    "ModelFinishReason",
    "ModelGateway",
    "ModelAttemptPolicy",
    "ModelGenerationCheckpointSink",
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
    "MockStrictModelProvider",
    "HttpxJsonTransport",
    "OPENAI_RESPONSES_MODEL_CAPABILITY_ID",
    "OPENAI_RESPONSES_PROVIDER_ID",
    "JsonHttpResponse",
    "JsonHttpTransport",
    "OpenAIResponsesModelProvider",
    "PreparedModelGeneration",
    "PreparedStructuredOutput",
    "StructuredGenerationAdapter",
]
