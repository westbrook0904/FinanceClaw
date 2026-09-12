"""One bounded structured model request with persistent preflight accounting."""

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from financeclaw.kernel.models import ModelProfile
from financeclaw.memory_worker.prompts import MemorySuggestions
from financeclaw.shared.llm.budget import ContextBudgetPlanner
from financeclaw.shared.llm.memory_profiles import memory_profile_fingerprint
from financeclaw.shared.memory.models import MemoryDerivationPermit, MemoryPermissionError
from financeclaw.shared.memory.repository import owner_filter
from financeclaw.shared.memory.tables import MemorySourceRow
from financeclaw.shared.outbox.models import OutboxEvent
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository


def validate_model_sources(session, actor, refs, profile, *, input_tokens=None, phase="extraction"):
    """Enforce immutable source classification, processing region and derivation capacity."""
    for ref in refs:
        source = session.scalar(
            select(MemorySourceRow).where(
                *owner_filter(MemorySourceRow, actor),
                MemorySourceRow.source_id == ref.source_id,
            )
        )
        if (
            source is None
            or source.data_classification not in profile.allowed_data_classes
            or source.processing_region not in profile.allowed_regions
        ):
            raise MemoryPermissionError(
                "memory model does not permit source classification or region"
            )
        if input_tokens is not None:
            if source.permit is None:
                raise MemoryPermissionError("memory derivation requires a bounded source permit")
            permit = MemoryDerivationPermit.model_validate(source.permit)
            output_cap = (
                permit.max_output_tokens
                if phase == "extraction"
                else permit.max_consolidation_output_tokens
            )
            if input_tokens > permit.max_input_tokens or profile.max_tokens > output_cap:
                raise MemoryPermissionError("memory model request exceeds source permit capacity")


class StructuredMemoryModel:
    """No graph, tools, implicit retries or model-selected processing budget."""

    def __init__(
        self,
        model,
        profile: ModelProfile,
        outbox: SqlAlchemyOutboxRepository,
        *,
        input_cap: int = 24000,
        timeout_seconds: float = 45,
    ) -> None:
        """Freeze profile and schema and use the same full-input counter as graph execution."""
        if not profile.supports_structured_output:
            raise ValueError("memory model must support structured output")
        self.profile = profile
        self.fingerprint = memory_profile_fingerprint(profile)
        self.outbox = outbox
        self.planner = ContextBudgetPlanner(profile, input_cap, safety_margin=256)
        self.timeout_seconds = min(timeout_seconds, profile.timeout_seconds)
        self.structured = model.with_structured_output(MemorySuggestions, include_raw=True)

    def messages(self, prompt: str, payload: dict):
        """Serialize source text in the user data envelope, separate from system policy."""
        return [
            SystemMessage(content=prompt),
            HumanMessage(
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            ),
        ]

    def estimate(self, prompt: str, payload: dict) -> int:
        """Include output Schema and complete source documents in partition planning."""
        return self.planner.estimate(
            self.messages(prompt, payload), output_schema=MemorySuggestions
        )

    async def generate(
        self,
        event: OutboxEvent,
        prompt: str,
        payload: dict,
        *,
        snapshot_id: str,
        max_attempts: int,
    ) -> MemorySuggestions:
        """Reserve before I/O and keep unknown responses charged across lease recovery."""
        messages = self.messages(prompt, payload)
        input_tokens = self.planner.check(messages, output_schema=MemorySuggestions)
        await asyncio.to_thread(
            self.outbox.reserve_model_attempt,
            event.event_id,
            claim_epoch=event.claim_epoch,
            max_attempts=max_attempts,
            input_tokens=input_tokens,
            output_tokens=self.profile.max_tokens,
            snapshot_id=snapshot_id,
        )
        async with asyncio.timeout(self.timeout_seconds):
            result = await self.structured.ainvoke(messages)
        raw = result.get("raw")
        usage = getattr(raw, "usage_metadata", None) or {}
        await asyncio.to_thread(
            self.outbox.record_model_usage,
            event.event_id,
            claim_epoch=event.claim_epoch,
            usage=usage,
        )
        if result.get("parsing_error") is not None or result.get("parsed") is None:
            raise ValueError("memory model returned invalid structured output")
        return MemorySuggestions.model_validate(result["parsed"])


class OfflineMemoryModel:
    """Explicit offline fixture: no generated memory unless test outputs are supplied."""

    def __init__(self, outputs=()) -> None:
        """Keep deterministic responses local to this fixture, never use a remote provider."""
        self.outputs = list(outputs)
        self.calls = []

    def with_structured_output(self, schema, *, include_raw=False):
        """Expose the same small model surface used by the production adapter."""
        return self

    async def ainvoke(self, messages):
        """Return a validated fixture output; an empty output means successful no-op."""
        self.calls.append(messages)
        output = self.outputs.pop(0) if self.outputs else MemorySuggestions()
        return {
            "parsed": MemorySuggestions.model_validate(output),
            "raw": None,
            "parsing_error": None,
        }
