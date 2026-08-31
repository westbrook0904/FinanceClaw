"""Router、Planner 与 Explorer 共用的 strict generation 入口。"""

from __future__ import annotations

from harness_contracts import InvocationContext, ModelReservationReceipt, RetryPolicy

from .contracts import GenerateRequest, GenerateResult
from .gateway import ModelGateway, ModelInvocationParent
from .preparation import (
    ModelAttemptPolicy,
    ModelGenerationCheckpointSink,
    PreparedModelGeneration,
)


class StructuredGenerationAdapter:
    """拒绝 legacy 请求，并把所有结构化生成统一交给 ModelGateway。"""

    def __init__(self, gateway: ModelGateway) -> None:
        if not isinstance(gateway, ModelGateway):
            raise TypeError("gateway must be ModelGateway")
        self._gateway = gateway

    @property
    def gateway(self) -> ModelGateway:
        return self._gateway

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        *,
        retry_policy: RetryPolicy | None = None,
        parent: ModelInvocationParent = None,
        trace_enabled: bool = True,
    ) -> GenerateResult:
        self._require_structured(request)
        return await self._gateway.generate(
            request,
            context,
            retry_policy=retry_policy,
            parent=parent,
            trace_enabled=trace_enabled,
        )

    async def prepare_generation(
        self,
        request: GenerateRequest,
        context: InvocationContext,
        attempt_policy: ModelAttemptPolicy | None = None,
    ) -> PreparedModelGeneration:
        self._require_structured(request)
        return await self._gateway.prepare_generation(request, context, attempt_policy)

    async def execute_prepared(
        self,
        prepared: PreparedModelGeneration,
        receipt: ModelReservationReceipt | None,
        context: InvocationContext,
        *,
        checkpoint_sink: ModelGenerationCheckpointSink | None = None,
    ) -> GenerateResult:
        return await self._gateway.execute_prepared(
            prepared,
            receipt,
            context,
            checkpoint_sink=checkpoint_sink,
        )

    @staticmethod
    def _require_structured(request: GenerateRequest) -> None:
        if not isinstance(request, GenerateRequest):
            raise TypeError("request must be GenerateRequest")
        if request.structured_output is None:
            raise ValueError("StructuredGenerationAdapter requires structured_output")
