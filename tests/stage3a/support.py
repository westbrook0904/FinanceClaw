"""Stage 3A Acceptance 的确定性 Provider 与构造辅助。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityExecutionProfile,
    CapabilityType,
    InvocationContext,
    ProviderDescriptor,
    Request,
    RequestInput,
    ResultEnvelope,
    ResultOutput,
)
from harness_registry import CapabilityRegistry
from harness_spi import ToolRequest, ToolSPI

type ProviderOutcome = ResultEnvelope | BaseException


class AcceptanceProviderTool(ToolSPI):
    """记录调用并按脚本返回结果的通用 Tool Provider。"""

    def __init__(
        self,
        capability_id: str,
        provider_name: str,
        *,
        profile: CapabilityExecutionProfile | None = None,
        outcomes: Sequence[ProviderOutcome] = (),
        started: asyncio.Event | None = None,
        block: bool = False,
    ) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
            execution_profile=profile or CapabilityExecutionProfile(),
        )
        self.provider_name = provider_name
        self.outcomes = tuple(outcomes)
        self.started = started
        self.block = block
        self.calls = 0
        self.contexts: list[InvocationContext] = []

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        self.calls += 1
        self.contexts.append(context)
        if self.started is not None:
            self.started.set()
        if self.block:
            await asyncio.Event().wait()

        if self.outcomes:
            outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        arguments = request.model_dump(mode="json")["arguments"]
        return ResultEnvelope.success(
            ResultOutput(
                type="json",
                data={"provider": self.provider_name, "arguments": arguments},
            )
        )


def register_provider(
    registry: CapabilityRegistry,
    provider: AcceptanceProviderTool,
    *,
    provider_id: str,
    priority: int,
    equivalence_group: str | None = None,
) -> None:
    capability = provider.descriptor()
    registry.register_provider(
        provider,
        descriptor=ProviderDescriptor(
            provider_id=provider_id,
            capability_id=capability.id,
            plugin_id=f"{provider_id}-plugin",
            implementation_version=capability.version,
            priority=priority,
            equivalence_group=equivalence_group,
        ),
    )


def provider_failure(
    code: str,
    *,
    retryable: bool = False,
    fallbackable: bool = True,
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        CapabilityError(
            "injected Stage 3A provider failure",
            code=code,
            retryable=retryable,
            fallbackable=fallbackable,
        ).to_detail()
    )


def make_request(request_id: str) -> Request:
    return Request(
        request_id=request_id,
        input=RequestInput(type="json", content={"query": "stage3a"}),
    )
