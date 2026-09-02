"""Conversation Journal context selection and ModelContextManifest persistence."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langsmith import traceable, tracing_context

from financeclaw.contracts import ExecutionContext
from financeclaw.conversation.context import ConversationContextBuilder
from financeclaw.conversation.models import ModelContextManifest
from financeclaw.conversation.repository import ConversationRepository
from financeclaw.tools import ToolCatalog

from .middleware import redact_sensitive

LOGGER = logging.getLogger("financeclaw.model_io")


@traceable(name="conversation.recall", run_type="retriever", tags=["stage:2"])
def trace_conversation_recall(
    *, conversation_id: str, recent_count: int, summary_count: int, historical_count: int
) -> None:
    del conversation_id, recent_count, summary_count, historical_count


@traceable(name="context.manifest.persist", run_type="chain", tags=["stage:2"])
def trace_manifest_persist(*, model_call_id: str, context_hash: str) -> None:
    del model_call_id, context_hash


class ConversationContextMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        builder: ConversationContextBuilder,
        repository: ConversationRepository,
        tool_catalog: ToolCatalog,
        agent_profile_version: str,
        model_profile_version: str,
        prompt_template_version: str,
        debug_full_io: bool,
    ) -> None:
        super().__init__()
        self.builder = builder
        self.repository = repository
        self.tool_catalog = tool_catalog
        self.agent_profile_version = agent_profile_version
        self.model_profile_version = model_profile_version
        self.prompt_template_version = prompt_template_version
        self.debug_full_io = debug_full_io

    @staticmethod
    def _context(request: ModelRequest) -> ExecutionContext:
        value = request.runtime.context
        if isinstance(value, ExecutionContext):
            return value
        if isinstance(value, Mapping):
            return ExecutionContext.model_validate(value)
        raise TypeError("trusted ExecutionContext is required")

    def _prepare(self, request: ModelRequest) -> tuple[ModelRequest, ModelContextManifest | None]:
        context = self._context(request)
        if context.conversation_id is None:
            return request, None
        system_prompt = ""
        if request.system_message is not None:
            content = request.system_message.content
            system_prompt = (
                content if isinstance(content, str) else json.dumps(content, default=str)
            )
        tools = list(request.tools)
        messages, selection = self.builder.build(
            context=context,
            runtime_messages=request.messages,
            system_prompt=system_prompt,
            tools=tools,
        )
        trace_conversation_recall(
            conversation_id=context.conversation_id,
            recent_count=len(selection.recent_message_ids),
            summary_count=len(selection.summary_ids),
            historical_count=len(selection.historical_message_ids),
        )
        journal_messages = {
            item.message_id: item for item in self.repository.list_messages(context.conversation_id)
        }
        recent_sequences = [
            journal_messages[item_id].sequence
            for item_id in selection.recent_message_ids
            if item_id in journal_messages
        ]
        exposed_tools: list[str] = []
        for tool in tools:
            tool_id = str(getattr(tool, "name", "unknown"))
            try:
                managed = self.tool_catalog.resolve(tool_id)
                exposed_tools.append(f"{managed.governance.tool_id}@{managed.governance.version}")
            except LookupError:
                exposed_tools.append(tool_id)
        model_call_id = f"model-call-{uuid4().hex}"
        manifest = ModelContextManifest(
            manifest_id=f"manifest-{uuid4().hex}",
            model_call_id=model_call_id,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            run_id=context.run_id,
            prompt_template_version=self.prompt_template_version,
            agent_profile_version=self.agent_profile_version,
            model_profile_version=self.model_profile_version,
            recent_message_start=min(recent_sequences) if recent_sequences else None,
            recent_message_end=max(recent_sequences) if recent_sequences else None,
            summary_ids=selection.summary_ids,
            historical_message_ids=selection.historical_message_ids,
            tool_result_refs=selection.tool_result_refs,
            exposed_tools=tuple(exposed_tools),
            input_token_count=selection.input_token_count,
            available_input_tokens=selection.available_input_tokens,
            omissions=selection.omissions,
            context_hash=selection.context_hash,
            created_at=datetime.now(UTC),
        )
        self.repository.save_manifest(manifest)
        trace_manifest_persist(
            model_call_id=manifest.model_call_id,
            context_hash=manifest.context_hash,
        )
        if self.debug_full_io:
            LOGGER.debug(
                "final_model_context=%s",
                json.dumps(
                    redact_sensitive(
                        {
                            **selection.debug_payload,
                            "model_call_id": model_call_id,
                            "manifest": manifest.model_dump(mode="json"),
                        }
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
        return request.override(messages=messages), manifest

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        prepared, manifest = self._prepare(request)
        metadata = (
            {"model_call_id": manifest.model_call_id, "context_hash": manifest.context_hash}
            if manifest is not None
            else {}
        )
        with tracing_context(metadata=metadata):
            return handler(prepared)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        prepared, manifest = await asyncio.to_thread(self._prepare, request)
        metadata = (
            {"model_call_id": manifest.model_call_id, "context_hash": manifest.context_hash}
            if manifest is not None
            else {}
        )
        with tracing_context(metadata=metadata):
            return await handler(prepared)
