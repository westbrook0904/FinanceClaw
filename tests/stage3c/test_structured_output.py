"""Stage 3C Step 2: strict structured output and reserved generation acceptance gate."""

from __future__ import annotations

import unittest
from collections.abc import Sequence

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ErrorCode,
    IdentityContext,
    InvocationContext,
    ModelAttemptAccounting,
    ModelGenerationAccounting,
    ModelProviderFeatures,
    ModelReservationReceipt,
    ModelSlotExecutionTicket,
    ModelUsage,
    PlanNodeRef,
    ProviderDescriptor,
    ProviderError,
    Request,
    RequestInput,
    RetryPolicy,
    StructuredOutputSpec,
    StructuredOutputStrictness,
    TenantContext,
    UnsupportedStructuredOutputBehavior,
)
from harness_model import (
    GenerateRequest,
    GenerateResult,
    GenerateStatus,
    ModelAttemptPolicy,
    ModelFinishReason,
    ModelGateway,
    ModelMessage,
    ModelOutput,
    ModelProvider,
    ModelResponseFormat,
    ModelRole,
    PreparedStructuredOutput,
)
from harness_model.schema import structured_schema_hash
from harness_registry import InMemoryCapabilityRegistry
from harness_runtime import DefaultInvocationContextFactory
from harness_trace import InMemoryTracer, SpanType
from pydantic import ValidationError

MODEL_ID = "stage3c.strict-model/v1"


def schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["kind", "payload"],
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["ok"]},
            "payload": {
                "type": "object",
                "required": ["count"],
                "additionalProperties": False,
                "properties": {"count": {"type": "integer"}},
            },
        },
    }


def structured_request(
    *,
    output_schema: dict[str, object] | None = None,
    max_output_tokens: int | None = 40,
) -> GenerateRequest:
    return GenerateRequest(
        model=MODEL_ID,
        messages=(ModelMessage(role=ModelRole.USER, content="TOP_SECRET_PROMPT"),),
        response_format=ModelResponseFormat.JSON,
        structured_output=StructuredOutputSpec(
            name="stage3c_result",
            schema=output_schema or schema(),
            strictness=StructuredOutputStrictness.REQUIRED,
            on_unsupported=UnsupportedStructuredOutputBehavior.FAIL,
        ),
        max_output_tokens=max_output_tokens,
    )


def context() -> InvocationContext:
    return DefaultInvocationContextFactory().create(
        Request(
            request_id="stage3c-structured-request",
            input=RequestInput(type="json", content={"goal": "strict"}),
        )
    )


def reserved_context(
    *,
    tenant_id: str | None = None,
    identity_subject: str | None = None,
) -> InvocationContext:
    updates: dict[str, object] = {
        "attributes": {
            "plan_id": "plan-1",
            "node_id": "node-1",
            "exploration_id": "explore-1",
        }
    }
    if tenant_id is not None:
        updates["tenant"] = TenantContext(tenant_id=tenant_id)
    if identity_subject is not None:
        updates["identity"] = IdentityContext(
            subject=identity_subject,
            scopes=frozenset({"model:generate"}),
        )
    return context().model_copy(update=updates)


def success_result(
    data: object,
    *,
    finish_reason: ModelFinishReason = ModelFinishReason.STOP,
    input_tokens: int = 5,
    output_tokens: int = 7,
    accounting_complete: bool = True,
) -> GenerateResult:
    usage = ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )
    return GenerateResult.success(
        ModelOutput(type=ModelResponseFormat.JSON, data=data),
        usage,
        finish_reason=finish_reason,
        attempt_accounting=ModelAttemptAccounting(
            usage=usage,
            complete=accounting_complete,
        ),
    )


class ScriptedStrictProvider(ModelProvider):
    def __init__(
        self,
        provider_id: str,
        outcomes: GenerateResult | Sequence[GenerateResult],
        *,
        strict: bool = True,
    ) -> None:
        self.provider_id = provider_id
        self.outcomes = (
            tuple(outcomes)
            if isinstance(outcomes, Sequence) and not isinstance(outcomes, GenerateResult)
            else (outcomes,)
        )
        self.feature_value = ModelProviderFeatures(
            json_object=True,
            json_schema=strict,
            json_schema_strict=strict,
            refusal_signal=strict,
            usage_tokens=True,
        )
        self.legacy_calls = 0
        self.outbound_calls = 0
        self.prepare_calls = 0

    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=MODEL_ID,
            name=MODEL_ID,
            type=CapabilityType.MODEL,
            version="1.0.0",
        )

    @property
    def features(self) -> ModelProviderFeatures:
        return self.feature_value

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        self.legacy_calls += 1
        return self._outcome(self.legacy_calls)

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput | None:
        self.prepare_calls += 1
        if not self.feature_value.json_schema_strict:
            return None
        return PreparedStructuredOutput(
            provider_id=self.provider_id,
            schema_hash=structured_schema_hash(spec),
            semantics_preserved=True,
            payload={"provider": self.provider_id},
        )

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        self.outbound_calls += 1
        return self._outcome(self.outbound_calls)

    def _outcome(self, call: int) -> GenerateResult:
        return self.outcomes[min(call - 1, len(self.outcomes) - 1)]


def register(
    registry: InMemoryCapabilityRegistry,
    provider: ScriptedStrictProvider,
    *,
    priority: int,
) -> None:
    registry.register_provider(
        provider,
        descriptor=ProviderDescriptor(
            provider_id=provider.provider_id,
            capability_id=MODEL_ID,
            plugin_id=f"{provider.provider_id}-plugin",
            implementation_version="1.0.0",
            priority=priority,
            equivalence_group="stage3c-strict",
        ),
    )


class MemoryCheckpointSink:
    def __init__(self, *, stale_ticket: bool = False, fail_terminal: bool = False) -> None:
        self.stale_ticket = stale_ticket
        self.fail_terminal = fail_terminal
        self.started: list[str] = []
        self.completed: list[tuple[str, str]] = []

    async def start_model_generation_slot(self, receipt, reservation, slot):
        self.started.append(slot.slot_id)
        return ModelSlotExecutionTicket(
            generation_id=reservation.generation_id,
            slot_id="stale-slot" if self.stale_ticket else slot.slot_id,
            reservation_hash=reservation.reservation_hash,
            committed_state_version=receipt.committed_state_version,
            scheduler_generation=receipt.scheduler_generation,
            owner_epoch=receipt.owner_epoch,
        )

    async def complete_model_generation_slot(
        self,
        receipt,
        ticket,
        accounting,
        outcome,
    ) -> None:
        if self.fail_terminal:
            raise RuntimeError("injected terminal CAS failure")
        self.completed.append((ticket.slot_id, outcome))


def receipt_for(prepared) -> ModelReservationReceipt:
    return ModelReservationReceipt(
        execution_ref=PlanNodeRef(plan_id="plan-1", node_id="node-1"),
        exploration_id="explore-1",
        generation_id=prepared.reservation.generation_id,
        reservation_hash=prepared.reservation.reservation_hash,
        committed_state_version=3,
        scheduler_generation=5,
        owner_epoch=7,
    )


class StructuredOutputContractTests(unittest.TestCase):
    def test_request_exclusivity_and_required_fail_closed_are_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            StructuredOutputSpec(
                name="unsafe-required",
                schema=schema(),
                strictness=StructuredOutputStrictness.REQUIRED,
                on_unsupported=UnsupportedStructuredOutputBehavior.JSON_OBJECT,
            )
        with self.assertRaises(ValidationError):
            GenerateRequest(
                model=MODEL_ID,
                messages=(ModelMessage(role=ModelRole.USER, content="x"),),
                response_format=ModelResponseFormat.JSON,
                response_schema=schema(),
                structured_output=StructuredOutputSpec(name="strict", schema=schema()),
            )

    def test_schema_resource_limits_and_remote_refs_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            StructuredOutputSpec(name="remote", schema={"$ref": "https://example.test/x"})
        with self.assertRaises(ValidationError):
            StructuredOutputSpec(
                name="enum",
                schema={"enum": list(range(300))},
            )

    def test_complete_accounting_requires_usage_and_generation_requires_attempts(self) -> None:
        with self.assertRaises(ValidationError):
            ModelAttemptAccounting(complete=True)
        with self.assertRaises(ValidationError):
            ModelGenerationAccounting(
                attempts=(),
                aggregate_usage=ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0),
                complete=True,
            )

    def test_plan_node_draft_schema_excludes_harness_owned_execution_fields(self) -> None:
        from harness_planning import PlanNodeDraft

        fields = PlanNodeDraft.model_fields
        for forbidden in (
            "plan_id",
            "revision",
            "retry_policy",
            "idempotency_key",
            "timeout_ms",
            "metadata",
        ):
            self.assertNotIn(forbidden, fields)
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValidationError):
                    PlanNodeDraft.model_validate(
                        {
                            "node_id": "one",
                            "capability_id": "tool.one/v1",
                            forbidden: "injected",
                        }
                    )


class StructuredOutputGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_2020_12_primitive_root_is_supported(self) -> None:
        registry = InMemoryCapabilityRegistry()
        provider = ScriptedStrictProvider(
            "primitive-root",
            success_result("approved"),
        )
        register(registry, provider, priority=100)

        result = await ModelGateway(registry, InMemoryTracer()).generate(
            structured_request(
                output_schema={"type": "string", "enum": ["approved"]},
            ),
            context(),
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.output.data, "approved")

    async def test_required_unsupported_fails_before_any_generation(self) -> None:
        registry = InMemoryCapabilityRegistry()
        legacy = ScriptedStrictProvider(
            "legacy-provider",
            success_result({"kind": "ok", "payload": {"count": 1}}),
            strict=False,
        )
        register(registry, legacy, priority=100)

        result = await ModelGateway(registry, InMemoryTracer()).generate(
            structured_request(), context()
        )

        self.assertEqual(result.status, GenerateStatus.FAILED)
        self.assertEqual(result.error.code, ErrorCode.MODEL_STRUCTURED_OUTPUT_UNSUPPORTED)
        self.assertEqual(legacy.legacy_calls + legacy.outbound_calls, 0)

    async def test_full_local_schema_validation_rejects_nested_enum_and_extra_fields(self) -> None:
        invalid_outputs = (
            {"kind": "ok", "payload": {"count": "one"}},
            {"kind": "wrong", "payload": {"count": 1}},
            {"kind": "ok", "payload": {"count": 1, "extra": True}},
        )
        for index, invalid in enumerate(invalid_outputs):
            with self.subTest(index=index):
                registry = InMemoryCapabilityRegistry()
                provider = ScriptedStrictProvider(f"strict-{index}", success_result(invalid))
                register(registry, provider, priority=100)
                result = await ModelGateway(registry, InMemoryTracer()).generate(
                    structured_request(), context()
                )
                self.assertEqual(result.status, GenerateStatus.FAILED)
                self.assertEqual(
                    result.error.code,
                    ErrorCode.MODEL_STRUCTURED_OUTPUT_INVALID,
                )
                self.assertEqual(provider.legacy_calls, 0)
                self.assertEqual(provider.outbound_calls, 1)

    async def test_finish_reasons_are_normalized_before_downstream_parsing(self) -> None:
        cases = (
            (ModelFinishReason.LENGTH, ErrorCode.MODEL_STRUCTURED_OUTPUT_TRUNCATED),
            (ModelFinishReason.REFUSAL, ErrorCode.MODEL_REFUSED),
            (ModelFinishReason.CONTENT_FILTER, ErrorCode.MODEL_CONTENT_FILTERED),
        )
        for index, (finish_reason, expected) in enumerate(cases):
            with self.subTest(finish_reason=finish_reason):
                registry = InMemoryCapabilityRegistry()
                provider = ScriptedStrictProvider(
                    f"finish-{index}",
                    success_result(
                        {"kind": "ok", "payload": {"count": 1}},
                        finish_reason=finish_reason,
                    ),
                )
                register(registry, provider, priority=100)
                result = await ModelGateway(registry, InMemoryTracer()).generate(
                    structured_request(), context()
                )
                self.assertEqual(result.status, GenerateStatus.FAILED)
                self.assertEqual(result.error.code, expected)
                self.assertIsNone(result.output)

    async def test_failed_then_fallback_success_aggregates_both_attempts(self) -> None:
        registry = InMemoryCapabilityRegistry()
        first_usage = ModelUsage(input_tokens=11, output_tokens=3, total_tokens=14)
        first_error = ProviderError(
            "safe injected failure",
            code="HARNESS.MODEL.INJECTED",
            fallbackable=True,
        )
        first = ScriptedStrictProvider(
            "strict-first",
            GenerateResult.failure(
                first_error.to_detail(),
                attempt_accounting=ModelAttemptAccounting(
                    usage=first_usage,
                    complete=True,
                ),
            ),
        )
        second = ScriptedStrictProvider(
            "strict-second",
            success_result(
                {"kind": "ok", "payload": {"count": 2}},
                input_tokens=5,
                output_tokens=7,
            ),
        )
        register(registry, first, priority=100)
        register(registry, second, priority=10)

        result = await ModelGateway(registry, InMemoryTracer()).generate(
            structured_request(), context()
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(first.outbound_calls, 1)
        self.assertEqual(second.outbound_calls, 1)
        self.assertIsNone(result.attempt_accounting)
        self.assertTrue(result.accounting.complete)
        self.assertEqual(len(result.accounting.attempts), 2)
        self.assertEqual(result.accounting.aggregate_usage.total_tokens, 26)

    async def test_only_schema_hash_is_observable(self) -> None:
        registry = InMemoryCapabilityRegistry()
        provider = ScriptedStrictProvider(
            "observable-strict",
            success_result({"kind": "ok", "payload": {"count": 1}}),
        )
        register(registry, provider, priority=100)
        tracer = InMemoryTracer()
        request = structured_request()

        result = await ModelGateway(registry, tracer).generate(request, context())

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        expected_hash = structured_schema_hash(request.structured_output)
        model_span = next(span for span in tracer.spans() if span.type is SpanType.MODEL)
        self.assertEqual(model_span.attributes["schema_hash"], expected_hash)
        serialized = "".join(span.model_dump_json() for span in tracer.spans())
        self.assertNotIn("TOP_SECRET_PROMPT", serialized)
        self.assertNotIn("additionalProperties", serialized)
        self.assertNotIn('"count":1', serialized)

    async def test_invalid_schema_definition_fails_before_generation(self) -> None:
        registry = InMemoryCapabilityRegistry()
        provider = ScriptedStrictProvider(
            "invalid-schema-provider",
            success_result({"kind": "ok", "payload": {"count": 1}}),
        )
        register(registry, provider, priority=100)

        result = await ModelGateway(registry, InMemoryTracer()).generate(
            structured_request(output_schema={"type": "not-a-json-schema-type"}),
            reserved_context(),
        )

        self.assertEqual(result.error.code, ErrorCode.MODEL_STRUCTURED_OUTPUT_SCHEMA_INVALID)
        self.assertEqual(provider.outbound_calls, 0)


class ReservedGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = InMemoryCapabilityRegistry()
        self.provider = ScriptedStrictProvider(
            "reserved-primary",
            success_result({"kind": "ok", "payload": {"count": 1}}),
        )
        register(self.registry, self.provider, priority=100)
        self.gateway = ModelGateway(self.registry, InMemoryTracer())

    async def prepare(self, invocation_context: InvocationContext | None = None):
        return await self.gateway.prepare_generation(
            structured_request(max_output_tokens=None),
            invocation_context or reserved_context(),
            ModelAttemptPolicy(
                retry_policy=RetryPolicy(
                    max_attempts=2,
                    initial_backoff_ms=0,
                    max_backoff_ms=0,
                ),
            ),
        )

    async def test_prepare_is_zero_outbound_and_reserves_every_retry_slot(self) -> None:
        prepared = await self.prepare()

        self.assertEqual(self.provider.outbound_calls + self.provider.legacy_calls, 0)
        self.assertEqual(len(prepared.reservation.slots), 2)
        self.assertEqual(
            [slot.provider_attempt for slot in prepared.reservation.slots],
            [1, 2],
        )

    async def test_reservation_covers_every_allowed_fallback_slot(self) -> None:
        backup = ScriptedStrictProvider(
            "reserved-backup",
            success_result({"kind": "ok", "payload": {"count": 2}}),
        )
        register(self.registry, backup, priority=10)

        prepared = await self.prepare()

        self.assertEqual(
            [slot.provider_id for slot in prepared.reservation.slots],
            [
                "reserved-primary",
                "reserved-primary",
                "reserved-backup",
                "reserved-backup",
            ],
        )
        self.assertEqual(self.provider.outbound_calls + backup.outbound_calls, 0)

    async def test_missing_receipt_or_started_cas_means_zero_outbound(self) -> None:
        prepared = await self.prepare()

        result = await self.gateway.execute_prepared(prepared, None, reserved_context())

        self.assertEqual(result.error.code, ErrorCode.MODEL_RECEIPT_MISMATCH)
        self.assertEqual(self.provider.outbound_calls, 0)

    async def test_stale_started_ticket_means_zero_outbound(self) -> None:
        prepared = await self.prepare()
        sink = MemoryCheckpointSink(stale_ticket=True)

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(),
            checkpoint_sink=sink,
        )

        self.assertEqual(result.error.code, ErrorCode.MODEL_RECEIPT_MISMATCH)
        self.assertEqual(self.provider.outbound_calls, 0)

    async def test_receipt_bound_to_another_execution_means_zero_outbound(self) -> None:
        prepared = await self.prepare()

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            context(),
            checkpoint_sink=MemoryCheckpointSink(),
        )

        self.assertEqual(result.error.code, ErrorCode.MODEL_RECEIPT_MISMATCH)
        self.assertEqual(self.provider.outbound_calls, 0)

    async def test_reservation_cannot_cross_tenant_or_identity_context(self) -> None:
        prepared = await self.prepare(
            reserved_context(tenant_id="tenant-a", identity_subject="user-a")
        )

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(tenant_id="tenant-b", identity_subject="user-a"),
            checkpoint_sink=MemoryCheckpointSink(),
        )

        self.assertEqual(result.error.code, ErrorCode.MODEL_RECEIPT_MISMATCH)
        self.assertEqual(self.provider.outbound_calls, 0)

    async def test_hot_replaced_provider_instance_orphans_reservation(self) -> None:
        prepared = await self.prepare()
        self.registry.unregister_provider(self.provider.provider_id)
        replacement = ScriptedStrictProvider(
            self.provider.provider_id,
            success_result({"kind": "ok", "payload": {"count": 2}}),
        )
        register(self.registry, replacement, priority=100)

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(),
            checkpoint_sink=MemoryCheckpointSink(),
        )

        self.assertEqual(result.error.code, ErrorCode.MODEL_GENERATION_ORPHANED)
        self.assertEqual(self.provider.outbound_calls, 0)
        self.assertEqual(replacement.outbound_calls, 0)

    async def test_valid_fencing_checkpoints_accounting_around_outbound(self) -> None:
        prepared = await self.prepare()
        sink = MemoryCheckpointSink()

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(),
            checkpoint_sink=sink,
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(self.provider.outbound_calls, 1)
        self.assertEqual(sink.started, [prepared.reservation.slots[0].slot_id])
        self.assertEqual(sink.completed[0][1], "completed")
        self.assertTrue(result.accounting.complete)

    async def test_terminal_checkpoint_failure_orphans_and_stops_fallback(self) -> None:
        prepared = await self.prepare()
        sink = MemoryCheckpointSink(fail_terminal=True)

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(),
            checkpoint_sink=sink,
        )

        self.assertEqual(result.error.code, ErrorCode.MODEL_GENERATION_ORPHANED)
        self.assertEqual(self.provider.outbound_calls, 1)

    async def test_usage_is_telemetry_and_does_not_gate_generation(self) -> None:
        self.provider.outcomes = (
            success_result(
                {"kind": "ok", "payload": {"count": 1}},
                input_tokens=101,
                output_tokens=7,
            ),
        )
        prepared = await self.prepare()
        sink = MemoryCheckpointSink()

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(),
            checkpoint_sink=sink,
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertEqual(result.accounting.aggregate_usage.total_tokens, 108)
        self.assertEqual(sink.completed[0][1], "completed")

    async def test_incomplete_usage_telemetry_does_not_gate_generation(self) -> None:
        self.provider.outcomes = (
            success_result(
                {"kind": "ok", "payload": {"count": 1}},
                accounting_complete=False,
            ),
        )
        prepared = await self.prepare()
        sink = MemoryCheckpointSink()

        result = await self.gateway.execute_prepared(
            prepared,
            receipt_for(prepared),
            reserved_context(),
            checkpoint_sink=sink,
        )

        self.assertEqual(result.status, GenerateStatus.SUCCESS)
        self.assertFalse(result.accounting.complete)
        self.assertEqual(sink.completed[0][1], "completed")


class RegistryFeatureSnapshotTests(unittest.TestCase):
    def test_registration_keeps_immutable_feature_snapshot(self) -> None:
        registry = InMemoryCapabilityRegistry()
        provider = ScriptedStrictProvider(
            "snapshot-provider",
            success_result({"kind": "ok", "payload": {"count": 1}}),
        )
        register(registry, provider, priority=100)
        registration = registry.get_provider(provider.provider_id)
        feature_hash = registration.model_features_hash

        provider.feature_value = ModelProviderFeatures()

        self.assertTrue(registration.model_features.json_schema_strict)
        self.assertEqual(registration.model_features_hash, feature_hash)
