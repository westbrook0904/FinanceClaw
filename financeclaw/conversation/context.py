"""Token-budgeted recent/relevant context selection for model calls."""

import json
import os
import re
import tempfile
from collections.abc import Sequence
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any

import tiktoken
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.contracts import ExecutionContext

from .models import ContextOmission, ContextSelection, ConversationMessage, ConversationSummary
from .repository import ConversationRepository

_TERM = re.compile(r"[A-Za-z0-9._-]{2,}|[\u4e00-\u9fff]{2,}")


class ContextBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_input_limit: int = Field(ge=1_024)
    reserved_output_tokens: int = Field(ge=64)
    system_policy_reserve: int = Field(ge=0)
    tool_schema_reserve: int = Field(ge=0)
    safety_margin: int = Field(ge=0)
    max_recent_messages: int = Field(default=12, ge=1, le=1_000)
    max_relevant_summaries: int = Field(default=4, ge=0, le=64)
    max_relevant_messages: int = Field(default=4, ge=0, le=64)

    @property
    def available_input_tokens(self) -> int:
        return (
            self.model_input_limit
            - self.reserved_output_tokens
            - self.system_policy_reserve
            - self.tool_schema_reserve
            - self.safety_margin
        )

    @model_validator(mode="after")
    def validate_available_budget(self) -> "ContextBudget":
        if self.available_input_tokens < 256:
            raise ValueError("context reserves leave fewer than 256 input tokens")
        return self


class TokenCounter:
    def __init__(self) -> None:
        # tiktoken may need to download its vocabulary on first use. Context
        # assembly must remain available in air-gapped workers and CI, so a
        # conservative deterministic counter is used when that cache is absent.
        self._encoding = None
        if _tiktoken_cache_available():
            try:
                self._encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:  # pragma: no cover - corrupt host cache
                self._encoding = None

    def text(self, value: str) -> int:
        if self._encoding is not None:
            return len(self._encoding.encode(value))
        return _estimated_tokens(value)

    def message(self, message: BaseMessage) -> int:
        payload = json.dumps(message.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return 4 + self.text(payload)

    def truncate(self, value: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        if self._encoding is not None:
            tokens = self._encoding.encode(value)
            if len(tokens) <= max_tokens:
                return value
            return self._encoding.decode(tokens[:max_tokens])
        if self.text(value) <= max_tokens:
            return value
        low, high = 0, len(value)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.text(value[:midpoint]) <= max_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        return value[:low]


def _estimated_tokens(value: str) -> int:
    """Conservative tokenizer fallback for offline startup."""
    # A BPE token cannot encode less than one source byte. Counting UTF-8
    # bytes therefore over-reserves rather than risking a model-window overrun.
    return len(value.encode("utf-8"))


def _tiktoken_cache_available() -> bool:
    cache_root = os.getenv("TIKTOKEN_CACHE_DIR") or os.getenv("DATA_GYM_CACHE_DIR")
    if cache_root == "":
        return False
    root = Path(cache_root) if cache_root else Path(tempfile.gettempdir()) / "data-gym-cache"
    source = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    return (root / sha1(source.encode()).hexdigest()).is_file()


class ConversationContextBuilder:
    def __init__(
        self,
        repository: ConversationRepository,
        budget: ContextBudget,
        *,
        counter: TokenCounter | None = None,
    ) -> None:
        self.repository = repository
        self.budget = budget
        self.counter = counter or TokenCounter()

    def build(
        self,
        *,
        context: ExecutionContext,
        runtime_messages: Sequence[AnyMessage],
        system_prompt: str,
        tools: Sequence[Any],
    ) -> tuple[list[AnyMessage], ContextSelection]:
        if context.conversation_id is None:
            raise ValueError("conversation_id is required for journal context selection")
        journal = self.repository.list_messages(context.conversation_id)
        summaries = self.repository.list_summaries(context.conversation_id)
        suffix = _current_runtime_suffix(runtime_messages)
        query = _latest_user_text(suffix) or (journal[-1].content if journal else "")
        serialized_tools = [
            {
                "name": getattr(tool, "name", str(tool)),
                "schema": getattr(tool, "args", None),
            }
            for tool in tools
        ]
        system_tokens = self.counter.text(system_prompt)
        tool_tokens = self.counter.text(
            json.dumps(serialized_tools, ensure_ascii=False, sort_keys=True, default=str)
        )
        reserve_overflow = max(0, system_tokens - self.budget.system_policy_reserve) + max(
            0, tool_tokens - self.budget.tool_schema_reserve
        )
        remaining = self.budget.available_input_tokens - reserve_overflow
        if remaining < 1:
            raise ValueError("mandatory system and tool context exceeds configured input budget")
        omissions: list[ContextOmission] = []

        suffix, suffix_tokens, suffix_omissions = self._fit_runtime_suffix(suffix, remaining)
        remaining -= suffix_tokens
        omissions.extend(suffix_omissions)

        duplicate_current = (
            journal[-1].message_id if journal and _matches_current(journal[-1], suffix) else None
        )
        eligible_journal = [item for item in journal if item.message_id != duplicate_current]
        recent_candidates = eligible_journal[-self.budget.max_recent_messages :]
        older = (
            eligible_journal[: -len(recent_candidates)] if recent_candidates else eligible_journal
        )

        recent_selected: list[ConversationMessage] = []
        for item in reversed(recent_candidates):
            tokens = self.counter.message(_to_message(item))
            if tokens <= remaining:
                recent_selected.append(item)
                remaining -= tokens
            else:
                omissions.append(
                    ContextOmission(
                        reason="token_budget",
                        item_type="message",
                        item_id=item.message_id,
                        token_count=tokens,
                    )
                )
        recent_selected.reverse()
        selected_recent_ids = {item.message_id for item in recent_selected}
        for item in recent_candidates:
            if item.message_id not in selected_recent_ids and not any(
                omission.item_id == item.message_id for omission in omissions
            ):
                omissions.append(
                    ContextOmission(
                        reason="recent_window",
                        item_type="message",
                        item_id=item.message_id,
                        token_count=self.counter.message(_to_message(item)),
                    )
                )

        relevant_summaries = _rank_summaries(query, summaries)[: self.budget.max_relevant_summaries]
        selected_summaries: list[ConversationSummary] = []
        for item in relevant_summaries:
            message = _summary_message(item)
            tokens = self.counter.message(message)
            if tokens <= remaining:
                selected_summaries.append(item)
                remaining -= tokens
            else:
                omissions.append(
                    ContextOmission(
                        reason="token_budget",
                        item_type="summary",
                        item_id=item.summary_id,
                        token_count=tokens,
                    )
                )

        relevant_old = _rank_messages(query, older)[: self.budget.max_relevant_messages]
        selected_old: list[ConversationMessage] = []
        for item in relevant_old:
            message = _to_message(item)
            tokens = self.counter.message(message)
            if tokens <= remaining:
                selected_old.append(item)
                remaining -= tokens
            else:
                omissions.append(
                    ContextOmission(
                        reason="token_budget",
                        item_type="message",
                        item_id=item.message_id,
                        token_count=tokens,
                    )
                )

        selected_old.sort(key=lambda item: item.sequence)
        messages: list[AnyMessage] = [
            *(_summary_message(item) for item in selected_summaries),
            *(_to_message(item) for item in selected_old),
            *(_to_message(item) for item in recent_selected),
            *suffix,
        ]
        input_tokens = (
            system_tokens + tool_tokens + sum(self.counter.message(item) for item in messages)
        )
        canonical = json.dumps(
            {
                "system": system_prompt,
                "messages": [item.model_dump(mode="json") for item in messages],
                "tools": serialized_tools,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        context_hash = sha256(canonical.encode()).hexdigest()
        tool_refs = tuple(
            str(item.additional_kwargs["artifact_ref"]["artifact_id"])
            for item in suffix
            if isinstance(item, ToolMessage)
            and isinstance(item.additional_kwargs.get("artifact_ref"), dict)
        )
        for item in suffix:
            reference = item.additional_kwargs.get("artifact_ref")
            if isinstance(item, ToolMessage) and isinstance(reference, dict):
                omissions.append(
                    ContextOmission(
                        reason="artifact_offloaded",
                        item_type="tool_result",
                        item_id=str(reference.get("artifact_id", item.id or "artifact")),
                        token_count=max(0, int(reference.get("size_bytes", 0)) // 4),
                    )
                )
        selection = ContextSelection(
            recent_message_ids=tuple(item.message_id for item in recent_selected),
            summary_ids=tuple(item.summary_id for item in selected_summaries),
            historical_message_ids=tuple(item.message_id for item in selected_old),
            tool_result_refs=tool_refs,
            input_token_count=input_tokens,
            available_input_tokens=self.budget.available_input_tokens,
            omissions=tuple(omissions),
            context_hash=context_hash,
            debug_payload={
                "system_prompt": system_prompt,
                "messages": [item.model_dump(mode="json") for item in messages],
                "tools": serialized_tools,
                "token_budget": self.budget.model_dump(mode="json"),
            },
        )
        return messages, selection

    def _fit_runtime_suffix(
        self, suffix: list[AnyMessage], remaining: int
    ) -> tuple[list[AnyMessage], int, list[ContextOmission]]:
        total = sum(self.counter.message(item) for item in suffix)
        if total <= remaining:
            return suffix, total, []
        fitted = list(suffix)
        omissions: list[ContextOmission] = []
        for index, item in enumerate(fitted):
            tokens = self.counter.message(item)
            if total <= remaining:
                break
            if isinstance(item, (HumanMessage, ToolMessage)) and isinstance(item.content, str):
                content_tokens = self.counter.text(item.content)
                message_overhead = tokens - content_tokens
                target = max(0, remaining - (total - tokens) - message_overhead)
                truncated = self.counter.truncate(item.content, target)
                fitted[index] = item.model_copy(update={"content": truncated})
                new_tokens = self.counter.message(fitted[index])
                total -= tokens - new_tokens
                omissions.append(
                    ContextOmission(
                        reason=(
                            "current_input_truncated"
                            if isinstance(item, HumanMessage)
                            else "token_budget"
                        ),
                        item_type=(
                            "current_input" if isinstance(item, HumanMessage) else "tool_result"
                        ),
                        item_id=str(getattr(item, "id", None) or f"runtime-{index}"),
                        token_count=tokens - new_tokens,
                    )
                )
        if total > remaining:
            raise ValueError("mandatory current model context exceeds configured input budget")
        return fitted, total, omissions


def _current_runtime_suffix(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    last_human = 0
    for index, item in enumerate(messages):
        if isinstance(item, HumanMessage):
            last_human = index
    return list(messages[last_human:])


def _latest_user_text(messages: Sequence[AnyMessage]) -> str:
    for item in reversed(messages):
        if isinstance(item, HumanMessage) and isinstance(item.content, str):
            return item.content
    return ""


def _matches_current(message: ConversationMessage, suffix: Sequence[AnyMessage]) -> bool:
    return bool(
        suffix
        and isinstance(suffix[0], HumanMessage)
        and isinstance(suffix[0].content, str)
        and message.role.value == "user"
        and message.content == suffix[0].content
    )


def _to_message(item: ConversationMessage) -> AnyMessage:
    if item.role.value == "user":
        return HumanMessage(content=item.content, id=item.message_id)
    return AIMessage(content=item.content, id=item.message_id)


def _summary_message(item: ConversationSummary) -> SystemMessage:
    return SystemMessage(
        content=(
            f"Historical conversation summary (sequences {item.start_sequence}-"
            f"{item.end_sequence}; historical facts may be stale): {item.summary_content}"
        ),
        id=item.summary_id,
    )


def _terms(value: str) -> set[str]:
    return {term.lower() for term in _TERM.findall(value)}


def _score(query: str, value: str) -> tuple[int, int]:
    overlap = _terms(query).intersection(_terms(value))
    return len(overlap), len(value)


def _rank_summaries(
    query: str, summaries: Sequence[ConversationSummary]
) -> list[ConversationSummary]:
    scored = [(item, _score(query, item.summary_content)) for item in summaries]
    return [
        item
        for item, score in sorted(scored, key=lambda pair: pair[1], reverse=True)
        if score[0] > 0
    ]


def _rank_messages(
    query: str, messages: Sequence[ConversationMessage]
) -> list[ConversationMessage]:
    scored = [(item, _score(query, item.content)) for item in messages]
    return [
        item
        for item, score in sorted(scored, key=lambda pair: pair[1], reverse=True)
        if score[0] > 0
    ]
