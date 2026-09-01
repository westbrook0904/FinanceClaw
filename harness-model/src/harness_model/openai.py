"""OpenAI Responses API 的无 SDK、非流式 ModelProvider adapter。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
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


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    status_code: int
    body: Mapping[str, object]


class JsonHttpTransport(Protocol):
    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> JsonHttpResponse: ...


class HttpxJsonTransport:
    """可取消的异步 JSON POST；重试和 fallback 仍由 Harness 治理。"""

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> JsonHttpResponse:
        try:
            import httpx
        except ImportError as exc:
            raise ProviderError(
                "HTTPX is required for OpenAI Responses API calls",
                code="HARNESS.MODEL.OPENAI_TRANSPORT_UNAVAILABLE",
                fallbackable=True,
            ) from exc
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                url,
                headers=dict(headers),
                json=dict(payload),
            )
        try:
            body = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                "OpenAI Responses API returned invalid JSON",
                code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
                retryable=response.status_code >= 500,
                fallbackable=True,
            ) from exc
        if not isinstance(body, Mapping):
            raise ProviderError(
                "OpenAI Responses API returned a non-object response",
                code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
                retryable=response.status_code >= 500,
                fallbackable=True,
            )
        return JsonHttpResponse(status_code=response.status_code, body=dict(body))


class OpenAIResponsesModelProvider(ModelProvider):
    """把 Harness GenerateRequest 映射到 OpenAI ``POST /v1/responses``。"""

    def __init__(
        self,
        *,
        api_key: str,
        openai_model: str,
        model_capability_id: str = OPENAI_RESPONSES_MODEL_CAPABILITY_ID,
        provider_id: str = OPENAI_RESPONSES_PROVIDER_ID,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        organization: str | None = None,
        project: str | None = None,
        transport: JsonHttpTransport | None = None,
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
            or timeout_seconds > 300
        ):
            raise ValueError("timeout_seconds must be greater than zero and at most 300")
        if organization is not None:
            organization = _non_empty("organization", organization)
        if project is not None:
            project = _non_empty("project", project)
        if transport is not None and not hasattr(transport, "post_json"):
            raise TypeError("transport must implement post_json")
        self._timeout_seconds = float(timeout_seconds)
        self._organization = organization
        self._project = project
        self._transport = transport or HttpxJsonTransport()
        self._descriptor = CapabilityDescriptor(
            id=model_capability_id,
            name="OpenAI Responses model generation",
            type=CapabilityType.MODEL,
            version="1.0.0",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            execution_profile=CapabilityExecutionProfile(
                side_effect=SideEffectType.NONE,
                egress=EgressType.EXTERNAL,
            ),
            tags=frozenset({"model", "openai", "responses", "real-provider"}),
            metadata={"api": "responses", "store": False},
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
        payload = {
            "type": "json_schema",
            "name": spec.name,
            "schema": spec.model_dump(mode="json")["schema"],
            "strict": True,
        }
        return PreparedStructuredOutput(
            provider_id=self._provider_id,
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
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
        return await self._generate(request, response_format=prepared.payload)

    async def _generate(
        self,
        request: GenerateRequest,
        *,
        response_format: Mapping[str, object] | None,
    ) -> GenerateResult:
        payload: dict[str, object] = {
            "model": self._openai_model,
            "input": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "store": False,
            "temperature": request.temperature,
        }
        if response_format is not None:
            payload["text"] = {"format": dict(response_format)}
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens

        try:
            response = await self._transport.post_json(
                f"{self._base_url}/responses",
                headers=self._headers(),
                payload=payload,
                timeout_seconds=self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "OpenAI Responses API request failed",
                code="HARNESS.MODEL.OPENAI_TRANSPORT_FAILED",
                details={"cause_type": type(exc).__name__},
                retryable=True,
                fallbackable=True,
            ) from exc

        if not 200 <= response.status_code < 300:
            return self._http_failure(response)
        return self._success_result(request, response.body)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._organization is not None:
            headers["OpenAI-Organization"] = self._organization
        if self._project is not None:
            headers["OpenAI-Project"] = self._project
        return headers

    def _http_failure(self, response: JsonHttpResponse) -> GenerateResult:
        api_code = _api_error_code(response.body)
        retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
        error = ProviderError(
            "OpenAI Responses API returned an error",
            code="HARNESS.MODEL.OPENAI_HTTP_ERROR",
            details={
                "http_status": response.status_code,
                **({"api_error_code": api_code} if api_code is not None else {}),
            },
            retryable=retryable,
            fallbackable=True,
        )
        return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)

    def _success_result(
        self,
        request: GenerateRequest,
        body: Mapping[str, object],
    ) -> GenerateResult:
        status = body.get("status")
        if status == "incomplete":
            reason = _nested_string(body.get("incomplete_details"), "reason")
            finish_reason = {
                "max_output_tokens": ModelFinishReason.LENGTH,
                "content_filter": ModelFinishReason.CONTENT_FILTER,
            }.get(reason)
            if finish_reason is not None:
                usage = _usage(body)
                output_data: object = (
                    "" if request.response_format is ModelResponseFormat.TEXT else {}
                )
                return GenerateResult.success(
                    ModelOutput(type=request.response_format, data=output_data),
                    usage,
                    finish_reason=finish_reason,
                    provider_id=self._provider_id,
                    attempt_accounting=ModelAttemptAccounting(usage=usage, complete=True),
                    metadata=_response_metadata(body),
                )
            error = ProviderError(
                "OpenAI response was incomplete",
                code="HARNESS.MODEL.OPENAI_INCOMPLETE",
                details={**({"reason": reason} if reason is not None else {})},
                retryable=True,
                fallbackable=True,
            )
            return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)
        if status != "completed":
            error = ProviderError(
                "OpenAI response did not complete",
                code="HARNESS.MODEL.OPENAI_INCOMPLETE",
                details={"status": str(status)[:64]},
                retryable=status in {"queued", "in_progress"},
                fallbackable=True,
            )
            return GenerateResult.failure(error.to_detail(), provider_id=self._provider_id)

        usage = _usage(body)
        refusal = _output_content(body, "refusal")
        if refusal is not None:
            output_data: object = "" if request.response_format is ModelResponseFormat.TEXT else {}
            return GenerateResult.success(
                ModelOutput(type=request.response_format, data=output_data),
                usage,
                finish_reason=ModelFinishReason.REFUSAL,
                provider_id=self._provider_id,
                attempt_accounting=ModelAttemptAccounting(usage=usage, complete=True),
                metadata=_response_metadata(body),
            )

        text = _output_text(body)
        if text is None:
            error = ProviderError(
                "OpenAI response did not contain output text",
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
                    "OpenAI response contained invalid JSON output",
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
            metadata=_response_metadata(body),
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


def _nested_string(value: object, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _api_error_code(body: Mapping[str, object]) -> str | None:
    value = _nested_string(body.get("error"), "code")
    return value[:128] if value is not None else None


def _output_content(body: Mapping[str, object], content_type: str) -> str | None:
    output = body.get("output")
    if not isinstance(output, list):
        return None
    values: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != content_type:
                continue
            value = part.get("text" if content_type == "output_text" else "refusal")
            if isinstance(value, str):
                values.append(value)
    return "".join(values) if values else None


def _output_text(body: Mapping[str, object]) -> str | None:
    text = _output_content(body, "output_text")
    if text is not None:
        return text
    value = body.get("output_text")
    return value if isinstance(value, str) else None


def _usage(body: Mapping[str, object]) -> ModelUsage:
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        raise ProviderError(
            "OpenAI response did not contain token usage",
            code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
            fallbackable=True,
        )
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        raise ProviderError(
            "OpenAI response contained invalid token usage",
            code="HARNESS.MODEL.OPENAI_INVALID_RESPONSE",
            fallbackable=True,
        )
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _response_metadata(body: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {"api": "responses", "store": False}
    for source, target in (("id", "response_id"), ("model", "model")):
        value = body.get(source)
        if isinstance(value, str) and value:
            metadata[target] = value[:256]
    return metadata
