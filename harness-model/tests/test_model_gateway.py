"""Step 8 ModelProvider / ModelGateway 的行为验收。"""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from harness_contracts import (
    ProviderDescriptor,
    ProviderHealthStatus,
    Request,
    RequestInput,
    RetryPolicy,
)
from harness_events import ExecutionEventName, InMemoryEventBus
from harness_model import (
    DEFAULT_MODEL_CAPABILITY_ID,
    GenerateRequest,
    GenerateStatus,
    MockBackupModel,
    MockFastModel,
    MockQualityModel,
    ModelGateway,
    ModelMessage,
    ModelResponseFormat,
    ModelRole,
)
from harness_registry import InMemoryCapabilityRegistry
from harness_runtime import DefaultInvocationContextFactory
from harness_selection import EligibilityPipeline, PrioritySelector, StaticHealthSource
from harness_trace import InMemoryTracer, SpanStatus, SpanType

QUALITY_PROVIDER_ID = "quality-provider"
FAST_PROVIDER_ID = "fast-provider"
BACKUP_PROVIDER_ID = "backup-provider"


def make_context():
    request = Request(input=RequestInput(type="text", content="hello model"))
    return DefaultInvocationContextFactory().create(request)


def make_request(
    *,
    response_format: ModelResponseFormat = ModelResponseFormat.TEXT,
    response_schema: dict[str, object] | None = None,
) -> GenerateRequest:
    return GenerateRequest(
        model=DEFAULT_MODEL_CAPABILITY_ID,
        messages=(ModelMessage(role=ModelRole.USER, content="hello model"),),
        response_format=response_format,
        response_schema=response_schema,
    )


def register(
    registry: InMemoryCapabilityRegistry,
    provider,
    provider_id: str,
    *,
    priority: int,
) -> None:
    registry.register_provider(
        provider,
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=DEFAULT_MODEL_CAPABILITY_ID,
            plugin_id="mock-models",
            implementation_version="1.0.0",
            priority=priority,
        ),
    )


class ModelContractTests(unittest.TestCase):
    def test_structured_output_and_usage_round_trip_as_frozen_contracts(self) -> None:
        request = make_request(
            response_format=ModelResponseFormat.JSON,
            response_schema={
                "type": "object",
                "required": ["provider", "content"],
            },
        )

        self.assertIsInstance(request.response_schema, Mapping)
        self.assertEqual(request.model_dump(mode="json")["response_schema"]["type"], "object")


class ModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = InMemoryCapabilityRegistry()
        self.tracer = InMemoryTracer()
        self.events = InMemoryEventBus()

    def gateway(self, *, selector: PrioritySelector | None = None) -> ModelGateway:
        return ModelGateway(
            self.registry,
            self.tracer,
            provider_selector=selector,
            event_publisher=self.events,
        )

    async def test_quality_model_is_selected_and_provider_identity_is_returned(self) -> None:
        fast = MockFastModel()
        quality = MockQualityModel()
        register(self.registry, fast, FAST_PROVIDER_ID, priority=20)
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)

        result = await self.gateway().generate(make_request(), make_context())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.provider_id, QUALITY_PROVIDER_ID)
        self.assertEqual(quality.calls, 1)
        self.assertEqual(fast.calls, 0)
        self.assertEqual(result.usage.total_tokens, 5)
        self.assertEqual(
            result.usage.total_tokens,
            result.usage.input_tokens + result.usage.output_tokens,
        )

    async def test_quality_model_timeout_is_normalized_and_traced(self) -> None:
        quality = MockQualityModel(delay_ms=50)
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)

        result = await self.gateway().generate(
            make_request(),
            make_context(),
            timeout_ms=5,
        )

        self.assertEqual(result.status, GenerateStatus.FAILED)
        self.assertEqual(result.provider_id, QUALITY_PROVIDER_ID)
        self.assertEqual(result.error.code, "HARNESS.TIMEOUT")
        self.assertTrue(result.error.retryable)
        model_spans = [span for span in self.tracer.spans() if span.type is SpanType.MODEL]
        self.assertEqual(len(model_spans), 1)
        self.assertEqual(model_spans[0].status, SpanStatus.ERROR)
        self.assertEqual(model_spans[0].error.code, "HARNESS.TIMEOUT")

    async def test_model_failure_trace_does_not_copy_raw_provider_message(self) -> None:
        quality = MockQualityModel(failures_before_success=1)
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)

        result = await self.gateway().generate(make_request(), make_context())

        self.assertEqual(result.status, GenerateStatus.FAILED)
        model_span = next(span for span in self.tracer.spans() if span.type is SpanType.MODEL)
        self.assertEqual(model_span.status, SpanStatus.ERROR)
        self.assertEqual(model_span.error.code, "HARNESS.MODEL.MOCK_FAILURE")
        self.assertEqual(model_span.error.message, "model generation failed")
        self.assertNotIn("temporarily unavailable", model_span.model_dump_json())

    async def test_quality_timeout_can_fall_back_to_backup_model(self) -> None:
        quality = MockQualityModel(delay_ms=50)
        backup = MockBackupModel()
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)
        register(self.registry, backup, BACKUP_PROVIDER_ID, priority=10)

        result = await self.gateway().generate(
            make_request(),
            make_context(),
            timeout_ms=5,
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.provider_id, BACKUP_PROVIDER_ID)
        self.assertEqual(quality.calls, 1)
        self.assertEqual(backup.calls, 1)
        fallback = next(
            event
            for event in self.events.events()
            if event.name is ExecutionEventName.PROVIDER_FALLBACK
        )
        self.assertEqual(fallback.attributes["source_provider_id"], QUALITY_PROVIDER_ID)
        self.assertEqual(fallback.attributes["target_provider_id"], BACKUP_PROVIDER_ID)

    async def test_same_provider_retry_succeeds_before_fallback(self) -> None:
        quality = MockQualityModel(failures_before_success=1)
        backup = MockBackupModel()
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)
        register(self.registry, backup, BACKUP_PROVIDER_ID, priority=10)

        result = await self.gateway().generate(
            make_request(),
            make_context(),
            retry_policy=RetryPolicy(
                max_attempts=2,
                initial_backoff_ms=0,
                max_backoff_ms=0,
            ),
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.provider_id, QUALITY_PROVIDER_ID)
        self.assertEqual(quality.calls, 2)
        self.assertEqual(backup.calls, 0)
        retry_events = [
            event
            for event in self.events.events()
            if event.name is ExecutionEventName.PROVIDER_RETRYING
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0].attributes["provider_id"], QUALITY_PROVIDER_ID)

    async def test_exhausted_quality_model_falls_back_to_backup(self) -> None:
        quality = MockQualityModel(failures_before_success=2)
        backup = MockBackupModel()
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)
        register(self.registry, backup, BACKUP_PROVIDER_ID, priority=10)

        result = await self.gateway().generate(make_request(), make_context())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.provider_id, BACKUP_PROVIDER_ID)
        self.assertEqual(quality.calls, 1)
        self.assertEqual(backup.calls, 1)
        fallback_events = [
            event
            for event in self.events.events()
            if event.name is ExecutionEventName.PROVIDER_FALLBACK
        ]
        self.assertEqual(len(fallback_events), 1)
        self.assertEqual(fallback_events[0].attributes["source_provider_id"], QUALITY_PROVIDER_ID)
        self.assertEqual(fallback_events[0].attributes["target_provider_id"], BACKUP_PROVIDER_ID)

    async def test_structured_output_and_usage_metadata_are_preserved(self) -> None:
        quality = MockQualityModel()
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)
        request = make_request(
            response_format=ModelResponseFormat.JSON,
            response_schema={
                "type": "object",
                "required": ["provider", "content", "score"],
                "properties": {
                    "provider": {"type": "string"},
                    "content": {"type": "string"},
                    "score": {"type": "integer"},
                },
            },
        )

        result = await self.gateway().generate(request, make_context())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.output.type, ModelResponseFormat.JSON)
        self.assertEqual(result.output.data["content"], "hello model")
        self.assertEqual(result.output.data["score"], 0)
        self.assertTrue(result.metadata["mock"])
        self.assertGreater(result.usage.input_tokens, 0)
        self.assertGreater(result.usage.output_tokens, 0)

    async def test_unhealthy_quality_model_is_rejected_by_shared_health_pipeline(self) -> None:
        quality = MockQualityModel()
        backup = MockBackupModel()
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)
        register(self.registry, backup, BACKUP_PROVIDER_ID, priority=10)
        selector = PrioritySelector(
            EligibilityPipeline(
                StaticHealthSource(
                    {
                        QUALITY_PROVIDER_ID: ProviderHealthStatus.UNHEALTHY,
                        BACKUP_PROVIDER_ID: ProviderHealthStatus.HEALTHY,
                    }
                )
            )
        )

        result = await self.gateway(selector=selector).generate(
            make_request(),
            make_context(),
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.provider_id, BACKUP_PROVIDER_ID)
        self.assertEqual(quality.calls, 0)
        self.assertEqual(backup.calls, 1)

    async def test_provider_selection_and_model_attempts_share_trace(self) -> None:
        quality = MockQualityModel(failures_before_success=1)
        backup = MockBackupModel()
        register(self.registry, quality, QUALITY_PROVIDER_ID, priority=100)
        register(self.registry, backup, BACKUP_PROVIDER_ID, priority=10)

        result = await self.gateway().generate(make_request(), make_context())

        self.assertIsNotNone(result.trace_id)
        spans = self.tracer.spans(trace_id=result.trace_id)
        self.assertTrue(any(span.type is SpanType.REGISTRY_RESOLVE for span in spans))
        selection_spans = [span for span in spans if span.type is SpanType.PROVIDER_SELECT]
        model_spans = [span for span in spans if span.type is SpanType.MODEL]
        self.assertEqual(len(selection_spans), 2)
        self.assertEqual(len(model_spans), 2)
        self.assertEqual(model_spans[0].attributes["provider_id"], QUALITY_PROVIDER_ID)
        self.assertEqual(model_spans[1].attributes["provider_id"], BACKUP_PROVIDER_ID)
        self.assertEqual(model_spans[1].attributes["provider_attempt"], 2)
        self.assertEqual(quality.contexts[0].trace_context.trace_id, result.trace_id)
