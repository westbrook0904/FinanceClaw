"""FinanceClaw Agent Foundation 的 Context Engineering Pipeline。"""

from .assembler import ContextAssembler
from .canonical import canonical_hash, canonical_json, context_item_facts, stable_item_id
from .models import ContextBundle, ContextCollection
from .pipeline import ContextPipeline
from .policy import ContextPolicy
from .projector import ContextProjector
from .prompt import ContextPrompt, PromptBuilder
from .source import (
    CapabilityCatalogContextSource,
    ContextSource,
    RequestContextSource,
    StaticContextEntry,
    StaticContextSource,
)

__all__ = [
    "CapabilityCatalogContextSource",
    "ContextAssembler",
    "ContextBundle",
    "ContextCollection",
    "ContextPipeline",
    "ContextPolicy",
    "ContextProjector",
    "ContextPrompt",
    "ContextSource",
    "PromptBuilder",
    "RequestContextSource",
    "StaticContextEntry",
    "StaticContextSource",
    "canonical_hash",
    "canonical_json",
    "context_item_facts",
    "stable_item_id",
]
