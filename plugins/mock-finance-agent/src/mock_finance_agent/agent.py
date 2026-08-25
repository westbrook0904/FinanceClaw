"""用于证明财经业务可以完全位于 Harness Core 之外的模拟 Agent。"""

from __future__ import annotations

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    ResultEnvelope,
    ResultOutput,
)
from harness_spi import AgentRequest, AgentSPI


class MockFinanceAgent(AgentSPI):
    """返回确定性的模拟财经结果，不访问真实数据源或执行真实分析。"""

    _descriptor = CapabilityDescriptor(
        id="finance.mock-query/v1",
        name="Mock Finance Query",
        type=CapabilityType.AGENT,
        version="1.0.0",
        tags=frozenset({"example", "finance", "local", "mock"}),
        metadata={"mock": True, "business_plugin": True},
    )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        input_payload = request.input.model_dump(mode="json")
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data={
                    "mock": True,
                    "message": "mock finance agent executed",
                    "input": input_payload,
                },
            ),
            metadata={
                "capability_id": self._descriptor.id,
                "request_id": context.request.request_id,
                "data_source": "none",
            },
        )
