"""Stage 3A ModelGateway 的确定性内存 Mock Provider。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    EgressType,
    InvocationContext,
    ModelAttemptAccounting,
    ModelProviderFeatures,
    NormalizedCost,
    NormalizedCostRate,
    ProviderError,
    SideEffectType,
    StructuredOutputSpec,
)

from .contracts import (
    GenerateRequest,
    GenerateResult,
    ModelFinishReason,
    ModelOutput,
    ModelResponseFormat,
    ModelUsage,
)
from .preparation import PreparedStructuredOutput
from .provider import ModelProvider
from .schema import structured_schema_hash

DEFAULT_MODEL_CAPABILITY_ID = "model.generate/v1"


class MockModelProvider(ModelProvider):
    """可配置延迟与瞬时失败的确定性 ModelProvider。"""

    provider_identity = "mock-model"

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL_CAPABILITY_ID,
        delay_ms: int = 0,
        failures_before_success: int = 0,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise TypeError("model_id must be a non-empty string")
        if not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or delay_ms < 0:
            raise TypeError("delay_ms must be a non-negative integer")
        if (
            not isinstance(failures_before_success, int)
            or isinstance(failures_before_success, bool)
            or failures_before_success < 0
        ):
            raise TypeError("failures_before_success must be a non-negative integer")

        self._descriptor = CapabilityDescriptor(
            id=model_id,
            name="Model generation",
            type=CapabilityType.MODEL,
            version="1.0.0",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            execution_profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.NONE,
                egress=EgressType.EXTERNAL,
            ),
            tags=frozenset({"model", "mock"}),
        )
        self._delay_ms = delay_ms
        self._failures_remaining = failures_before_success
        self.calls = 0
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        return await self._generate_result(request, context)

    async def _generate_result(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        if not isinstance(request, GenerateRequest):
            raise TypeError("request must be GenerateRequest")
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be InvocationContext")

        self.calls += 1
        self.contexts.append(context)
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000)
        if self._failures_remaining:
            self._failures_remaining -= 1
            error = ProviderError(
                "mock model is temporarily unavailable",
                code="HARNESS.MODEL.MOCK_FAILURE",
                details={"provider_identity": self.provider_identity},
                retryable=True,
                fallbackable=True,
            )
            return GenerateResult.failure(
                error.to_detail(),
                provider_id=self.provider_identity,
            )

        prompt = request.messages[-1].content
        output = self._output(request, prompt)
        input_tokens = sum(_token_count(message.content) for message in request.messages)
        output_tokens = _token_count(_serialized_output(output))
        return GenerateResult.success(
            output,
            ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason=ModelFinishReason.STOP,
            provider_id=self.provider_identity,
            metadata={"mock": True},
        )

    def _output(self, request: GenerateRequest, prompt: str) -> ModelOutput:
        if request.response_format is ModelResponseFormat.TEXT:
            return ModelOutput(
                type=ModelResponseFormat.TEXT,
                data=f"{self.provider_identity}: {prompt}",
            )

        data: dict[str, object] = {
            "provider": self.provider_identity,
            "content": prompt,
        }
        schema = (
            request.structured_output.schema
            if request.structured_output is not None
            else request.response_schema
        )
        if schema is not None:
            properties = schema.get("properties")
            required = schema.get("required")
            if isinstance(properties, Mapping) and isinstance(required, tuple | list):
                for key in required:
                    if isinstance(key, str) and key not in data:
                        data[key] = _example_value(properties.get(key))
        return ModelOutput(type=ModelResponseFormat.JSON, data=data)


class MockFastModel(MockModelProvider):
    provider_identity = "mock-fast-model"


class MockQualityModel(MockModelProvider):
    provider_identity = "mock-quality-model"


class MockBackupModel(MockModelProvider):
    provider_identity = "mock-backup-model"


class MockStrictModelProvider(MockModelProvider):
    """支持 strict schema 编译、sound token bound 与可选成本计量的 Mock。"""

    def __init__(
        self,
        *,
        provider_id: str,
        cost_rate: NormalizedCostRate | None = None,
        **kwargs: object,
    ) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise TypeError("provider_id must be a non-empty string")
        super().__init__(**kwargs)
        self._provider_id = provider_id
        self._cost_rate = cost_rate

    @property
    def features(self) -> ModelProviderFeatures:
        return ModelProviderFeatures(
            json_object=True,
            json_schema=True,
            json_schema_strict=True,
            refusal_signal=True,
            usage_tokens=True,
            normalized_cost=self._cost_rate is not None,
            cost_rate=self._cost_rate,
        )

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput:
        return PreparedStructuredOutput(
            provider_id=self._provider_id,
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
            payload={"mock": "strict"},
        )

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        if prepared.provider_id != self._provider_id:
            raise ProviderError("prepared output belongs to another mock provider")
        result = await self._generate_result(request, context)
        if result.status.value != "success" or self._cost_rate is None or result.usage is None:
            return result
        cost = NormalizedCost(
            unit=self._cost_rate.unit,
            amount=(
                result.usage.input_tokens * self._cost_rate.max_input_token_cost
                + result.usage.output_tokens * self._cost_rate.max_output_token_cost
                + self._cost_rate.max_request_cost
            ),
        )
        return result.model_copy(
            update={
                "attempt_accounting": ModelAttemptAccounting(
                    usage=result.usage,
                    normalized_cost=cost,
                    complete=True,
                )
            }
        )

    def bound_input_tokens(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
    ) -> int:
        schema_bytes = 0
        if request.structured_output is not None:
            schema_bytes = len(
                json.dumps(
                    request.structured_output.model_dump(mode="json")["schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            )
        message_bytes = sum(len(item.content.encode("utf-8")) for item in request.messages)
        return message_bytes + schema_bytes + 64


def _serialized_output(output: ModelOutput) -> str:
    if isinstance(output.data, str):
        return output.data
    return json.dumps(output.model_dump(mode="json")["data"], sort_keys=True)


def _token_count(value: str) -> int:
    return max(1, len(value.split()))


def _example_value(schema: object) -> object:
    if not isinstance(schema, Mapping):
        return None
    schema_type = schema.get("type")
    if schema_type == "string":
        return "mock"
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return False
    if schema_type == "array":
        return []
    if schema_type == "object":
        return {}
    return None
