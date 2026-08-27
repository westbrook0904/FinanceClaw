"""Stage 3A ModelGateway 公共 Composition Root 验收。"""

from __future__ import annotations

import unittest

from harness_bootstrap import build_harness
from harness_contracts import ProviderDescriptor, Request, RequestInput
from harness_events import ExecutionEventName
from harness_model import (
    DEFAULT_MODEL_CAPABILITY_ID,
    GenerateRequest,
    GenerateStatus,
    MockBackupModel,
    MockQualityModel,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
)
from harness_trace import SpanStatus, SpanType


class ModelFabricAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_quality_timeout_falls_back_with_structured_usage_and_trace(self) -> None:
        quality = MockQualityModel(delay_ms=50)
        backup = MockBackupModel()
        app = build_harness(entry_point_group=None)
        app.registry.register_provider(
            quality,
            descriptor=ProviderDescriptor(
                provider_id="model-quality",
                capability_id=DEFAULT_MODEL_CAPABILITY_ID,
                plugin_id="quality-model-plugin",
                implementation_version="1.0.0",
                priority=100,
            ),
        )
        app.registry.register_provider(
            backup,
            descriptor=ProviderDescriptor(
                provider_id="model-backup",
                capability_id=DEFAULT_MODEL_CAPABILITY_ID,
                plugin_id="backup-model-plugin",
                implementation_version="1.0.0",
                priority=50,
            ),
        )
        invocation = Request(
            request_id="stage3a-model-request",
            input=RequestInput(type="text", content="summarize provider fabric"),
        )
        context = app.components.context_factory.create(invocation)
        request = GenerateRequest(
            model=DEFAULT_MODEL_CAPABILITY_ID,
            messages=(
                ModelMessage(
                    role=ModelRole.USER,
                    content="summarize provider fabric",
                ),
            ),
            response_format=ModelResponseFormat.JSON,
            response_schema={
                "type": "object",
                "required": ["provider", "content"],
            },
        )

        await app.start()
        try:
            result = await app.model_gateway.generate(
                request,
                context,
                timeout_ms=5,
            )
        finally:
            await app.shutdown()

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.provider_id, "model-backup")
        self.assertEqual(result.output.type, ModelResponseFormat.JSON)
        self.assertEqual(result.output.data["content"], "summarize provider fabric")
        self.assertGreater(result.usage.input_tokens, 0)
        self.assertEqual(
            result.usage.total_tokens,
            result.usage.input_tokens + result.usage.output_tokens,
        )
        self.assertEqual(quality.calls, 1)
        self.assertEqual(backup.calls, 1)

        fallback = next(
            event
            for event in app.event_publisher.events()
            if event.name is ExecutionEventName.PROVIDER_FALLBACK
        )
        self.assertEqual(fallback.attributes["source_provider_id"], "model-quality")
        self.assertEqual(fallback.attributes["target_provider_id"], "model-backup")
        model_spans = [span for span in app.tracer.spans() if span.type is SpanType.MODEL]
        self.assertEqual(
            [span.attributes["provider_id"] for span in model_spans],
            ["model-quality", "model-backup"],
        )
        self.assertEqual(model_spans[0].status, SpanStatus.ERROR)
        self.assertEqual(model_spans[0].error.code, "HARNESS.TIMEOUT")
        self.assertEqual(model_spans[1].status, SpanStatus.OK)
        self.assertEqual(result.trace_id, model_spans[0].trace_id)
