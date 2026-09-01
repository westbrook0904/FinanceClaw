"""ContextProjection 到模型 messages/payload 的唯一安全投影。"""

from __future__ import annotations

from dataclasses import dataclass

from harness_contracts import (
    ContextProjection,
    ContextSourceKind,
    ContextTrustTier,
)


@dataclass(frozen=True, slots=True)
class ContextPrompt:
    system_instructions: tuple[str, ...]
    payload: dict[str, object]


class PromptBuilder:
    def build(self, projection: ContextProjection) -> ContextPrompt:
        if not isinstance(projection, ContextProjection):
            raise TypeError("projection must be ContextProjection")

        instructions: list[str] = []
        data_items: list[dict[str, object]] = []
        for item in projection.items:
            if (
                item.source.source_kind is ContextSourceKind.SYSTEM_INSTRUCTION
                and item.trust_tier is ContextTrustTier.SYSTEM
            ):
                if not isinstance(item.content, str):
                    raise TypeError("system instruction context content must be a string")
                instructions.append(item.content)
                continue
            payload = item.model_dump(mode="json")
            data_items.append(
                {
                    "kind": item.kind,
                    "source_kind": payload["source"]["source_kind"],
                    "trust_tier": item.trust_tier.value,
                    "content": payload["content"],
                }
            )

        return ContextPrompt(
            system_instructions=tuple(instructions),
            payload={
                "consumer": projection.consumer.value,
                "items": data_items,
            },
        )
