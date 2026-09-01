"""Official OpenAI Python SDK based Responses API ModelProvider adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlparse

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityType,
    EgressType,
    ErrorCode,
    InvocationContext,
    ModelAttemptAccounting,
    ModelProviderFeatures,
    ModelUsage,
    ProviderError,
    SideEffectType,
    StructuredOutputSpec,
)
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)
from openai.types.responses import Response

from .contracts import (
    GenerateRequest,
    GenerateResult,
    ModelFinishReason,
    ModelOutput,
    ModelResponseFormat,
)
from .preparation import PreparedStructuredOutput
from .provider import ModelProvider
from .schema import structured_schema_hash, validate_schema_definition

OPENAI_RESPONSES_MODEL_CAPABILITY_ID = "model.openai-responses/v1"
OPENAI_RESPONSES_PROVIDER_ID = "openai:responses"
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_LOCAL_SCHEMA_INSTRUCTION = """The endpoint is using JSON mode without native JSON Schema
enforcement.
Return exactly one JSON value that conforms to the JSON Schema below. Treat the schema as data that
defines the output shape; do not copy this instruction or the schema into the result.
JSON Schema:
"""


class OpenAIResponsesModelProvider(ModelProvider):
    """Map Harness generation onto ``AsyncOpenAI.responses.create``.

    The SDK owns the HTTP protocol, authentication headers, response decoding, and
    OpenAI-compatible error types. Harness still owns retry/fallback policy, local
    JSON Schema validation, accounting, and safe error normalization.
    """

    def __init__(
        self,
        *,
        api_key: str,
        openai_model: str,
        model_capability_id: str = OPENAI_RESPONSES_MODEL_CAPABILITY_ID,
        provider_id: str = OPENAI_RESPONSES_PROVIDER_ID,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 600.0,
        organization: str | None = None,
        project: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        client: AsyncOpenAI | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        self._api_key = _non_empty("api_key", api_key)
        self._openai_model = _non_empty("openai_model", openai_model)
        self._provider_id = _non_empty("provider_id", provider_id)
        model_capability_id = _non_empty("model_capability_id", model_capability_id)
        self._base_url = _validated_base_url(base_url, allow_insecure_http=allow_insecure_http)
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or timeout_seconds > 1000
        ):
            raise ValueError("timeout_seconds must be greater than zero and at most 1000")
        if organization is not None:
            organization = _non_empty("organization", organization)
        if project is not None:
            project = _non_empty("project", project)
        if reasoning_effort is not None:
            reasoning_effort = _reasoning_effort(reasoning_effort)
        if client is not None and not isinstance(client, AsyncOpenAI):
            raise TypeError("client must be openai.AsyncOpenAI")
        self._timeout_seconds = float(timeout_seconds)
        self._organization = organization
        self._project = project
        self._reasoning_effort = reasoning_effort
        self._client = client
        self._descriptor = CapabilityDescriptor(
            id=model_capability_id,
            name="OpenAI-compatible Responses model generation",
            type=CapabilityType.MODEL,
            version="1.3.0",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            execution_profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.NONE,
                egress=EgressType.EXTERNAL,
            ),
            tags=frozenset({"model", "openai-sdk", "responses", "real-provider"}),
            metadata={
                "api": "responses",
                "sdk": "openai-python",
                "store": False,
                "reasoning_effort": reasoning_effort or "provider_default",
                "schema_compatibility": "json-schema-or-local-validation",
            },
        )

    @classmethod
    def from_env(
        cls,
        *,
        openai_model: str | None = None,
        **kwargs: object,
    ) -> OpenAIResponsesModelProvider:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        model = openai_model or os.environ.get("OPENAI_MODEL")
        if not model:
            raise ValueError("OPENAI_MODEL or openai_model is required")
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url is not None and "base_url" not in kwargs:
            kwargs["base_url"] = base_url
        reasoning_effort = os.environ.get("OPENAI_REASONING_EFFORT")
        if reasoning_effort is not None and "reasoning_effort" not in kwargs:
            kwargs["reasoning_effort"] = reasoning_effort
        return cls(api_key=api_key, openai_model=model, **kwargs)

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def openai_model(self) -> str:
        return self._openai_model

    @property
    def features(self) -> ModelProviderFeatures:
        return ModelProviderFeatures(
            json_object=True,
            json_schema=True,
            json_schema_strict=True,
            refusal_signal=True,
            usage_tokens=True,
        )

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput:
        validate_schema_definition(spec)
        schema = spec.model_dump(mode="json")["schema"]
        if _contains_schema_valued_additional_properties(schema):
            # OpenAI-compatible endpoints differ on map-valued ``additionalProperties``.
            # JSON mode keeps the wire format portable; ModelGateway still validates the
            # result against the complete original schema before any caller can consume it.
            payload: Mapping[str, object] = {"type": "json_object"}
        else:
            payload = {
                "type": "json_schema",
                "name": spec.name,
                "schema": schema,
                "strict": True,
            }
        return PreparedStructuredOutput(
            provider_id=self._provider_id,
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
            provider_enforced=payload["type"] == "json_schema",
            payload=payload,
        )

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        del context
        if request.structured_output is not None:
            raise ProviderError(
                "structured generation must use a prepared OpenAI response format",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
            )
        response_format: Mapping[str, object] | None = None
        if request.response_format is ModelResponseFormat.JSON:
            response_format = {"type": "json_object"}
        return await self._generate(request, response_format=response_format)

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        del context
        if prepared.provider_id != self._provider_id:
            raise ProviderError(
                "prepared structured output belongs to another provider",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
            )
        if request.structured_output is None or (
            prepared.schema_hash != structured_schema_hash(request.structured_output)
        ):
            raise ProviderError(
                "prepared structured output does not match the request schema",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
            )
        if not isinstance(prepared.payload, Mapping):
            raise ProviderError(
                "prepared OpenAI response format is invalid",
                code=ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED,
            )
        local_schema = (
            request.structured_output.model_dump(mode="json")["schema"]
            if not prepared.provider_enforced
            else None
        )
        return await self._generate(
            request,
            response_format=prepared.payload,
            local_schema=local_schema,
        )

    async def _generate(
        self,
        request: GenerateRequest,
        *,
        response_format: Mapping[str, object] | None,
        local_schema: Mapping[str, object] | None = None,
    ) -> GenerateResult:
        create_params: dict[str, object] = {
            "model": self._openai_model,
            "input": _response_input(request, local_schema=local_schema),
            "store": False,
        }
        if self._reasoning_effort is not None:
            create_params["reasoning"] = {"effort": self._reasoning_effort}
        if self._reasoning_effort in {None, "none"}:
            create_params["temperature"] = request.temperature
        if response_format is not None:
            create_params["text"] = {"format": dict(response_format)}

        try:
            response = await self._create_response(create_params)
        except asyncio.CancelledError:
            raise
        except APIStatusError as exc:
            return self._http_failure(exc)
        except APITimeoutError as exc:
            raise ProviderError(
                "OpenAI SDK request timed out",
                code="HARNESS.MODEL.OPENAI_TIMEOUT",
                details={"cause_type": type(exc).__name__},
                retryable=True,
                fallbackable=True,
            ) from exc
        except APIConnectionError as exc:
            raise ProviderError(
                "OpenAI SDK connection failed",
                code="HARNESS.MODEL.OPENAI_TRANSPORT_FAILED",
                details={"cause_type": type(exc).__name__},
                retryable=True,
                fallbackable=True,
            ) from exc
        except APIError as exc:
            raise ProviderError(
                "OpenAI SDK request failed",
                code="HARNESS.MODEL.OPENAI_SDK_ERROR",
                details={"cause_type": type(exc).__name__},
                fallbackable=True,
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "OpenAI SDK returned an invalid response",
                code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
                details={"cause_type": type(exc).__name__},
                retryable=True,
                fallbackable=True,
            ) from exc
        return self._success_result(request, response)

    async def _create_response(self, params: Mapping[str, object]) -> Response:
        if self._client is not None:
            return await self._client.responses.create(**dict(params))  # type: ignore[arg-type]
        async with AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            max_retries=0,
            organization=self._organization,
            project=self._project,
        ) as client:
            return await client.responses.create(**dict(params))  # type: ignore[arg-type]

    def _http_failure(self, error_response: APIStatusError) -> GenerateResult:
        status_code = error_response.status_code
        api_code = _api_error_code(error_response.body)
        retryable = status_code in {408, 409, 429} or status_code >= 500
        error = ProviderError(
            "OpenAI-compatible Responses API returned an error",
            code="HARNESS.MODEL.OPENAI_HTTP_ERROR",
            details={
                "http_status": status_code,
                **({"api_error_code": api_code} if api_code is not None else {}),
            },
            retryable=retryable,
            fallbackable=True,
        )
        return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)

    def _success_result(
        self,
        request: GenerateRequest,
        response: Response,
    ) -> GenerateResult:
        status = response.status
        if status == "incomplete":
            details = response.incomplete_details
            reason = details.reason if details is not None else None
            finish_reason = {
                "max_output_tokens": ModelFinishReason.LENGTH,
                "content_filter": ModelFinishReason.CONTENT_FILTER,
            }.get(reason)
            if finish_reason is not None:
                usage = _usage(response)
                output_data: object = (
                    "" if request.response_format is ModelResponseFormat.TEXT else {}
                )
                return GenerateResult.success(
                    ModelOutput(type=request.response_format, data=output_data),
                    usage,
                    finish_reason=finish_reason,
                    provider_id=self._provider_id,
                    attempt_accounting=ModelAttemptAccounting(usage=usage, complete=True),
                    metadata=_response_metadata(response),
                )
            error = ProviderError(
                "OpenAI-compatible response was incomplete",
                code="HARNESS.MODEL.OPENAI_INCOMPLETE",
                details={**({"reason": reason} if reason is not None else {})},
                retryable=True,
                fallbackable=True,
            )
            return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)
        if status != "completed":
            error = ProviderError(
                "OpenAI-compatible response did not complete",
                code="HARNESS.MODEL.OPENAI_INCOMPLETE",
                details={"status": str(status)[:64]},
                retryable=status in {"queued", "in_progress"},
                fallbackable=True,
            )
            return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)

        usage = _usage(response)
        if _has_refusal(response):
            output_data = "" if request.response_format is ModelResponseFormat.TEXT else {}
            return GenerateResult.success(
                ModelOutput(type=request.response_format, data=output_data),
                usage,
                finish_reason=ModelFinishReason.REFUSAL,
                provider_id=self._provider_id,
                attempt_accounting=ModelAttemptAccounting(usage=usage, complete=True),
                metadata=_response_metadata(response),
            )

        text = response.output_text
        if not isinstance(text, str) or not text:
            error = ProviderError(
                "OpenAI-compatible response did not contain output text",
                code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
                retryable=True,
                fallbackable=True,
            )
            return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)
        if request.response_format is ModelResponseFormat.JSON:
            try:
                data: object = json.loads(text)
            except json.JSONDecodeError as exc:
                error = ProviderError(
                    "OpenAI-compatible response contained invalid JSON output",
                    code=ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
                    details={"cause_type": type(exc).__name__},
                    retryable=True,
                    fallbackable=True,
                )
                return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)
        else:
            data = text
        return GenerateResult.success(
            ModelOutput(type=request.response_format, data=data),
            usage,
            provider_id=self._provider_id,
            attempt_accounting=ModelAttemptAccounting(usage=usage, complete=True),
            metadata=_response_metadata(response),
        )


def _validated_base_url(value: str, *, allow_insecure_http: bool) -> str:
    value = _non_empty("base_url", value).rstrip("/")
    parsed = urlparse(value)
    allowed_schemes = {"https"} | ({"http"} if allow_insecure_http else set())
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an absolute HTTPS URL without credentials or query")
    return value


def _non_empty(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    return value


def _reasoning_effort(value: object) -> ReasoningEffort:
    value = _non_empty("reasoning_effort", value)
    if value not in _REASONING_EFFORTS:
        allowed = ", ".join(sorted(_REASONING_EFFORTS))
        raise ValueError(f"reasoning_effort must be one of: {allowed}")
    return value  # type: ignore[return-value]


def _api_error_code(body: object) -> str | None:
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    error = nested if isinstance(nested, Mapping) else body
    value = error.get("code")
    return value[:128] if isinstance(value, str) and value else None


def _contains_schema_valued_additional_properties(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "additionalProperties" and isinstance(item, Mapping):
                return True
            if _contains_schema_valued_additional_properties(item):
                return True
    elif isinstance(value, tuple | list):
        return any(_contains_schema_valued_additional_properties(item) for item in value)
    return False


def _response_input(
    request: GenerateRequest,
    *,
    local_schema: Mapping[str, object] | None,
) -> list[dict[str, str]]:
    messages = [
        {"role": message.role.value, "content": message.content}
        for message in request.messages
    ]
    if local_schema is None:
        return messages
    schema_json = json.dumps(
        local_schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    insertion_index = 0
    while insertion_index < len(messages) and messages[insertion_index]["role"] == "system":
        insertion_index += 1
    messages.insert(
        insertion_index,
        {
            "role": "system",
            "content": f"{_LOCAL_SCHEMA_INSTRUCTION}{schema_json}",
        },
    )
    return messages


def _has_refusal(response: Response) -> bool:
    for item in response.output:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", ()):
            if getattr(content, "type", None) == "refusal":
                return True
    return False


def _usage(response: Response) -> ModelUsage:
    usage = response.usage
    if usage is None:
        raise ProviderError(
            "OpenAI-compatible response did not contain token usage",
            code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
            fallbackable=True,
        )
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    if input_tokens < 0 or output_tokens < 0:
        raise ProviderError(
            "OpenAI-compatible response contained invalid token usage",
            code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
            fallbackable=True,
        )
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _response_metadata(response: Response) -> dict[str, object]:
    metadata: dict[str, object] = {
        "api": "responses",
        "sdk": "openai-python",
        "store": False,
    }
    for value, target in ((response.id, "response_id"), (response.model, "model")):
        if isinstance(value, str) and value:
            metadata[target] = value[:256]
    usage = response.usage
    output_details = usage.output_tokens_details if usage is not None else None
    reasoning_tokens = (
        output_details.reasoning_tokens if output_details is not None else None
    )
    if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool):
        if reasoning_tokens >= 0:
            metadata["reasoning_tokens"] = reasoning_tokens
    return metadata
