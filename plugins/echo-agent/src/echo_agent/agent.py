"""阶段一用于验证 Agent 调用链的最小回显能力。"""

from __future__ import annotations

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    ResultEnvelope,
    ResultOutput,
)
from harness_spi import AgentRequest, AgentSPI


class EchoAgent(AgentSPI):
    """原样返回输入内容，不包含任何业务逻辑。"""

    _descriptor = CapabilityDescriptor(
        id="echo.reply/v1",
        name="Echo Reply",
        type=CapabilityType.AGENT,
        version="1.0.0",
        tags=frozenset({"example", "local", "echo"}),
        metadata={"deterministic": True},
    )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self,
        request: AgentRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        payload = request.input.model_dump(mode="json")
        return ResultEnvelope.success(
            ResultOutput(type=payload["type"], data=payload["content"]),
            metadata={
                "capability_id": self._descriptor.id,
                "request_id": context.request.request_id,
            },
        )
