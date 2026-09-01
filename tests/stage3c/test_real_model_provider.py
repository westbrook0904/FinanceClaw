"""Foundation F5 official OpenAI SDK ModelProvider offline acceptance tests."""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping

import httpx
from harness_contracts import (
    InvocationContext,
    Request,
    RequestInput,
    StructuredOutputSpec,
)
from harness_model import (
    GenerateRequest,
    GenerateStatus,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
    OpenAIResponsesModelProvider,
)
from openai import AsyncOpenAI


class RecordingResponsesEndpoint:
    def __init__(self, *, status_code: int, body: Mapping[str, object]) -> None:
        self.status_code = status_code
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "payload": payload,
            }
        )
        return httpx.Response(self.status_code, json=self.body, request=request)


def invocation() -> InvocationContext:
    return InvocationContext(request=Request(input=RequestInput(type="text", content="hello")))


def strict_request() -> GenerateRequest:
    return GenerateRequest(
        model="model.openai-responses/v1",
        messages=(
            ModelMessage(role=ModelRole.SYSTEM, content="Return one JSON object."),
            ModelMessage(role=ModelRole.USER, content="Evaluate this input."),
        ),
        response_format=ModelResponseFormat.JSON,
        structured_output=StructuredOutputSpec(
            name="f5_result",
            schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
        max_output_tokens=128,
    )


def response_body(
    text: str,
    *,
    response_id: str = "resp_f5",
    model: str = "gpt-test",
) -> dict[str, object]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
    }


class OpenAIResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def _provider(
        self,
        endpoint: RecordingResponsesEndpoint,
        *,
        provider_id: str = "openai:responses",
        reasoning_effort: str | None = None,
    ) -> OpenAIResponsesModelProvider:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
        client = AsyncOpenAI(
            api_key="test-secret-key",
            base_url="https://api.example.test/v1",
            http_client=http_client,
            max_retries=0,
        )
        self.addAsyncCleanup(client.close)
        return OpenAIResponsesModelProvider(
            api_key="test-secret-key",
            openai_model="gpt-test",
            provider_id=provider_id,
            base_url="https://api.example.test/v1",
            reasoning_effort=reasoning_effort,
            client=client,
        )

    async def test_strict_request_uses_sdk_and_parses_usage(self) -> None:
        endpoint = RecordingResponsesEndpoint(status_code=200, body=response_body('{"value":7}'))
        provider = await self._provider(endpoint, provider_id="f5-openai-provider")
        request = strict_request()
        prepared = provider.prepare_structured_output(request.structured_output)
        self.assertTrue(prepared.provider_enforced)

        result = await provider.generate_prepared(request, prepared, invocation())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.output.data, {"value": 7})
        self.assertEqual(result.usage.total_tokens, 15)
        self.assertEqual(result.metadata["response_id"], "resp_f5")
        self.assertEqual(result.metadata["sdk"], "openai-python")
        self.assertEqual(len(endpoint.calls), 1)
        outbound = endpoint.calls[0]
        self.assertEqual(outbound["url"], "https://api.example.test/v1/responses")
        self.assertEqual(outbound["headers"]["authorization"], "Bearer test-secret-key")
        payload = outbound["payload"]
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(
            payload["text"]["format"],
            {
                "type": "json_schema",
                "name": "f5_result",
                "schema": request.structured_output.model_dump(mode="json")["schema"],
                "strict": True,
            },
        )
        self.assertNotIn("test-secret-key", repr(provider))
        self.assertNotIn("test-secret-key", repr(result))

    async def test_reasoning_mode_returns_only_final_answer_and_safe_usage(self) -> None:
        body = response_body('{"value":9}', response_id="resp_reasoning")
        body["output"] = [
            {
                "id": "rs_1",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "content": [
                    {
                        "type": "reasoning_text",
                        "text": "private provider reasoning must not enter Harness output",
                    }
                ],
            },
            *body["output"],
        ]
        body["usage"] = {
            "input_tokens": 11,
            "output_tokens": 13,
            "total_tokens": 24,
            "output_tokens_details": {"reasoning_tokens": 9},
        }
        endpoint = RecordingResponsesEndpoint(status_code=200, body=body)
        provider = await self._provider(endpoint, reasoning_effort="high")
        request = strict_request()

        result = await provider.generate_prepared(
            request,
            provider.prepare_structured_output(request.structured_output),
            invocation(),
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.output.data, {"value": 9})
        self.assertEqual(result.usage.output_tokens, 13)
        self.assertEqual(result.metadata["reasoning_tokens"], 9)
        payload = endpoint.calls[0]["payload"]
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertNotIn("private provider reasoning", repr(result))

    async def test_reasoning_none_keeps_temperature(self) -> None:
        endpoint = RecordingResponsesEndpoint(status_code=200, body=response_body("done"))
        provider = await self._provider(endpoint, reasoning_effort="none")

        result = await provider.generate(
            GenerateRequest(
                model="model.openai-responses/v1",
                messages=(ModelMessage(role=ModelRole.USER, content="hello"),),
                temperature=0.25,
                max_output_tokens=1,
            ),
            invocation(),
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        payload = endpoint.calls[0]["payload"]
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["temperature"], 0.25)
        self.assertNotIn("max_output_tokens", payload)

    async def test_map_valued_additional_properties_uses_portable_json_mode(self) -> None:
        endpoint = RecordingResponsesEndpoint(
            status_code=200,
            body=response_body('{"values":{"answer":7}}'),
        )
        provider = await self._provider(endpoint)
        request = GenerateRequest(
            model="model.openai-responses/v1",
            messages=(ModelMessage(role=ModelRole.USER, content="Return JSON."),),
            response_format=ModelResponseFormat.JSON,
            structured_output=StructuredOutputSpec(
                name="map_result",
                schema={
                    "type": "object",
                    "properties": {
                        "values": {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        }
                    },
                    "required": ["values"],
                    "additionalProperties": False,
                },
            ),
        )

        prepared = provider.prepare_structured_output(request.structured_output)
        self.assertFalse(prepared.provider_enforced)
        result = await provider.generate_prepared(request, prepared, invocation())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        payload = endpoint.calls[0]["payload"]
        self.assertEqual(payload["text"]["format"], {"type": "json_object"})
        messages = payload["input"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn(
            '"additionalProperties":{"type":"integer"}',
            messages[0]["content"],
        )
        self.assertEqual(messages[1]["content"], "Return JSON.")

    async def test_http_error_is_sanitized_and_retry_classified(self) -> None:
        endpoint = RecordingResponsesEndpoint(
            status_code=429,
            body={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "do not expose provider diagnostic details",
                }
            },
        )
        provider = await self._provider(endpoint)

        result = await provider.generate(
            GenerateRequest(
                model="model.openai-responses/v1",
                messages=(ModelMessage(role=ModelRole.USER, content="hello"),),
            ),
            invocation(),
        )

        self.assertEqual(result.status, GenerateStatus.FAILED)
        self.assertEqual(result.error.code, "HARNESS.MODEL.OPENAI_HTTP_ERROR")
        self.assertTrue(result.error.retryable)
        self.assertTrue(result.error.fallbackable)
        self.assertEqual(result.error.details["http_status"], 429)
        self.assertEqual(result.error.details["api_error_code"], "rate_limit_exceeded")
        self.assertNotIn("diagnostic", repr(result))
        self.assertNotIn("test-secret-key", repr(result))

    async def test_refusal_is_preserved_as_finish_reason(self) -> None:
        body = response_body("")
        body["id"] = "resp_refusal"
        body["output"] = [
            {
                "id": "msg_refusal",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "refusal", "refusal": "cannot comply"}],
            }
        ]
        endpoint = RecordingResponsesEndpoint(status_code=200, body=body)
        provider = await self._provider(endpoint)
        request = strict_request()

        result = await provider.generate_prepared(
            request,
            provider.prepare_structured_output(request.structured_output),
            invocation(),
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.finish_reason.value, "refusal")

    def test_configuration_rejects_unsafe_or_ambiguous_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
            OpenAIResponsesModelProvider(
                api_key="key",
                openai_model="model",
                base_url="http://api.example.test/v1",
            )
        with self.assertRaisesRegex(ValueError, "surrounding whitespace"):
            OpenAIResponsesModelProvider(api_key=" key", openai_model="model")
        with self.assertRaisesRegex(ValueError, "reasoning_effort must be one of"):
            OpenAIResponsesModelProvider(
                api_key="key",
                openai_model="model",
                reasoning_effort="extreme",
            )


if __name__ == "__main__":
    unittest.main()
