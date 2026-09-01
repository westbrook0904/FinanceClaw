"""Foundation F5 真实 ModelProvider adapter 的离线契约验收。"""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from harness_contracts import (
    InvocationContext,
    Request,
    RequestInput,
    StructuredOutputSpec,
)
from harness_model import (
    GenerateRequest,
    GenerateStatus,
    JsonHttpResponse,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
    OpenAIResponsesModelProvider,
)


class RecordingTransport:
    def __init__(self, response: JsonHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> JsonHttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


def invocation() -> InvocationContext:
    return InvocationContext(request=Request(input=RequestInput(type="text", content="hello")))


def strict_request() -> GenerateRequest:
    return GenerateRequest(
        model="model.openai-responses/v1",
        messages=(
            ModelMessage(role=ModelRole.SYSTEM, content="Return one object."),
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


class OpenAIResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_request_maps_to_responses_and_parses_usage(self) -> None:
        transport = RecordingTransport(
            JsonHttpResponse(
                status_code=200,
                body={
                    "id": "resp_f5",
                    "model": "gpt-test",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": '{"value":7}'}],
                        }
                    ],
                    "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
                },
            )
        )
        provider = OpenAIResponsesModelProvider(
            api_key="test-secret-key",
            openai_model="gpt-test",
            provider_id="f5-openai-provider",
            transport=transport,
        )
        request = strict_request()
        prepared = provider.prepare_structured_output(request.structured_output)

        result = await provider.generate_prepared(request, prepared, invocation())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.output.data, {"value": 7})
        self.assertEqual(result.usage.total_tokens, 15)
        self.assertEqual(result.metadata["response_id"], "resp_f5")
        self.assertEqual(len(transport.calls), 1)
        outbound = transport.calls[0]
        self.assertEqual(outbound["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(outbound["headers"]["Authorization"], "Bearer test-secret-key")
        payload = outbound["payload"]
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["max_output_tokens"], 128)
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

    async def test_http_error_is_sanitized_and_retry_classified(self) -> None:
        transport = RecordingTransport(
            JsonHttpResponse(
                status_code=429,
                body={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "do not expose provider diagnostic details",
                    }
                },
            )
        )
        provider = OpenAIResponsesModelProvider(
            api_key="test-secret-key",
            openai_model="gpt-test",
            transport=transport,
        )

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
        transport = RecordingTransport(
            JsonHttpResponse(
                status_code=200,
                body={
                    "id": "resp_refusal",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "cannot comply"}],
                        }
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            )
        )
        provider = OpenAIResponsesModelProvider(
            api_key="test-secret-key",
            openai_model="gpt-test",
            transport=transport,
        )
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


if __name__ == "__main__":
    unittest.main()
