"""Persistent multi-turn conversation orchestration around Agent Server runs."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from langchain_core.messages import AIMessage

from financeclaw.agents import AgentProfileCatalog
from financeclaw.contracts import (
    AgentTarget,
    ApprovalDecision,
    ApprovalDecisionType,
    ConversationMessageResponse,
    ConversationMessagesResponse,
    ConversationResponse,
    ExecutionContext,
    RunAccepted,
    RunRequest,
    RunStatusResponse,
    StreamEvent,
)
from financeclaw.conversation import (
    ConversationConflict,
    ConversationNotFound,
    SqlAlchemyConversationRepository,
    SummaryService,
)
from financeclaw.conversation import (
    IdempotencyConflict as JournalIdempotencyConflict,
)

from .agent_server_client import AgentServerClient
from .run_service import IdempotencyConflict, RunNotFound


class ApprovalExpired(RuntimeError):
    """The checkpoint remains immutable, but an expired action cannot resume."""


class ConversationService:
    def __init__(
        self,
        client: AgentServerClient,
        repository: SqlAlchemyConversationRepository,
        agent_profiles: AgentProfileCatalog,
        *,
        summary_service: SummaryService | None = None,
        approval_timeout_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if approval_timeout_seconds < 0:
            raise ValueError("approval timeout cannot be negative")
        self.client = client
        self.repository = repository
        self.agent_profiles = agent_profiles
        self.summary_service = summary_service
        self.approval_timeout = timedelta(seconds=approval_timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        agent_id: str = "finance_agent",
        agent_profile_version: str | None = None,
    ) -> ConversationResponse:
        profile = self.agent_profiles.resolve(agent_id, agent_profile_version)
        conversation = await asyncio.to_thread(
            self.repository.create_conversation,
            tenant_id=tenant_id,
            subject_id=subject_id,
            agent_id=profile.agent_id,
            agent_profile_version=profile.version,
        )
        return _conversation_response(conversation)

    def get(self, conversation_id: str, *, tenant_id: str, subject_id: str) -> ConversationResponse:
        conversation = self.repository.get_owned(conversation_id, tenant_id, subject_id)
        return _conversation_response(conversation)

    def messages(
        self, conversation_id: str, *, tenant_id: str, subject_id: str
    ) -> ConversationMessagesResponse:
        self.repository.get_owned(conversation_id, tenant_id, subject_id)
        messages = self.repository.list_messages(conversation_id)
        return ConversationMessagesResponse(
            conversation_id=conversation_id,
            messages=tuple(
                ConversationMessageResponse(
                    message_id=item.message_id,
                    turn_id=item.turn_id,
                    sequence=item.sequence,
                    parent_message_id=item.parent_message_id,
                    role=item.role.value,
                    content=item.content,
                    created_at=item.created_at.isoformat(),
                )
                for item in messages
            ),
        )

    async def start_turn(
        self,
        request: RunRequest,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
    ) -> RunAccepted:
        if request.conversation_id is None:
            raise ValueError("conversation_id is required")
        conversation = await asyncio.to_thread(
            self.repository.get_owned,
            request.conversation_id,
            tenant_id,
            subject_id,
        )
        if request.target is not None:
            if not isinstance(request.target, AgentTarget):
                raise ConversationConflict(
                    "conversation turns only support the pinned Agent target"
                )
            if request.target.agent_id != conversation.agent_id or (
                request.target.version is not None
                and request.target.version != conversation.agent_profile_version
            ):
                raise ConversationConflict("conversation AgentProfile is pinned and cannot change")
        request_hash = sha256(
            json.dumps(
                {
                    "conversation_id": conversation.conversation_id,
                    "message": request.message,
                    "agent_id": conversation.agent_id,
                    "agent_profile_version": conversation.agent_profile_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        try:
            turn, _, replay = await asyncio.to_thread(
                self.repository.begin_turn,
                conversation_id=conversation.conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                message=request.message,
                target_type="agent",
                target_id=conversation.agent_id,
                target_version=conversation.agent_profile_version,
            )
        except JournalIdempotencyConflict as exc:
            raise IdempotencyConflict(str(exc)) from exc
        if turn.server_run_id is None:
            await self.client.create_thread(conversation.agent_thread_id)
            context = ExecutionContext(
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
            )
            server_run = (
                await self.client.find_run(
                    thread_id=conversation.agent_thread_id,
                    application_run_id=turn.run_id,
                )
                if replay
                else None
            )
            if server_run is None:
                server_run = await self.client.create_run(
                    thread_id=conversation.agent_thread_id,
                    assistant_id="finance_agent",
                    input={"messages": [{"role": "user", "content": request.message}]},
                    context=context.model_dump(mode="json"),
                    metadata=_server_metadata(
                        context,
                        stage="3",
                        conversation_id=conversation.conversation_id,
                        agent_profile_version=conversation.agent_profile_version,
                    ),
                )
            turn = await asyncio.to_thread(
                self.repository.bind_server_run,
                turn.turn_id,
                server_run.run_id,
                server_run.status,
            )
        return RunAccepted(
            run_id=turn.run_id,
            thread_id=conversation.agent_thread_id,
            status=turn.status.value,
            target_kind="agent",
            idempotent_replay=replay,
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
        )

    async def status(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        if turn.server_run_id is None:
            return RunStatusResponse(
                run_id=run_id,
                thread_id=conversation.agent_thread_id,
                status=turn.status.value,
            )
        server = await self.client.get_run(
            thread_id=conversation.agent_thread_id, run_id=turn.server_run_id
        )
        server_status = str(server.get("status", turn.status.value))
        output: Mapping[str, Any] | None = None
        if server_status in {"success", "completed"}:
            output = await self.client.join_run(
                thread_id=conversation.agent_thread_id, run_id=turn.server_run_id
            )
            final_content = _final_assistant_content(output)
            await asyncio.to_thread(
                self._record_completed,
                run_id,
                conversation.conversation_id,
                final_content,
            )
            status = "completed"
        elif server_status in {"error", "failed"}:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "failed")
            status = "failed"
        elif server_status == "interrupted":
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "interrupted")
            status = "interrupted"
        else:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, server_status)
            status = server_status
        serializable_output = _jsonable_output(output)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=conversation.agent_thread_id,
            status=status,
            output=serializable_output,
        )

    async def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
    ) -> RunStatusResponse:
        try:
            turn, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        created_at = turn.created_at
        if created_at.tzinfo is None:
            # SQLite drops timezone metadata; application timestamps are UTC by
            # invariant, so restore the marker before comparing the deadline.
            created_at = created_at.replace(tzinfo=UTC)
        if self._clock() >= created_at + self.approval_timeout:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, "failed")
            raise ApprovalExpired("approval window has expired; start a new memory proposal")
        mapped: dict[str, Any] = {"type": decision.type.value}
        if decision.reason is not None:
            mapped["message"] = decision.reason
        if decision.arguments_hash is not None:
            mapped["arguments_hash"] = decision.arguments_hash
        if decision.type is ApprovalDecisionType.EDIT:
            mapped["arguments"] = decision.arguments
        context = ExecutionContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            run_id=run_id,
        )
        result = await self.client.resume_run(
            thread_id=conversation.agent_thread_id,
            assistant_id="finance_agent",
            command={"resume": {"decisions": [mapped]}},
            context=context.model_dump(mode="json"),
            metadata=_server_metadata(context, stage="3"),
        )
        status = "interrupted" if result.get("__interrupt__") else "completed"
        if status == "completed":
            final_content = _final_assistant_content(result)
            await asyncio.to_thread(
                self._record_completed,
                run_id,
                conversation.conversation_id,
                final_content,
            )
        else:
            await asyncio.to_thread(self.repository.update_turn_status, run_id, status)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=conversation.agent_thread_id,
            status=status,
            output=_jsonable_output(result),
        )

    async def stream(
        self, run_id: str, *, tenant_id: str, subject_id: str
    ) -> AsyncIterator[StreamEvent]:
        try:
            _, conversation = await asyncio.to_thread(
                self._owned_turn_and_conversation,
                run_id,
                tenant_id,
                subject_id,
            )
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        async for part in self.client.stream_thread(
            thread_id=conversation.agent_thread_id, assistant_id="finance_agent"
        ):
            if isinstance(part, Mapping):
                yield StreamEvent(
                    event=str(part.get("event", "message")),
                    data=part.get("data", dict(part)),
                )
            else:
                yield StreamEvent(
                    event=str(getattr(part, "event", "message")),
                    data=getattr(part, "data", repr(part)),
                )

    def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        try:
            turn = self.repository.get_turn_owned(run_id, tenant_id, subject_id)
            self.repository.get_owned(turn.conversation_id, tenant_id, subject_id)
        except ConversationNotFound as exc:
            raise RunNotFound(str(exc)) from exc

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        reconciled: list[str] = []
        turns = await asyncio.to_thread(self.repository.list_incomplete_turns)
        for turn in turns:
            if turn.server_run_id is None:
                continue
            await self.status(turn.run_id, tenant_id=turn.tenant_id, subject_id=turn.subject_id)
            reconciled.append(turn.run_id)
        return tuple(reconciled)

    def _owned_turn_and_conversation(
        self, run_id: str, tenant_id: str, subject_id: str
    ) -> tuple[Any, Any]:
        turn = self.repository.get_turn_owned(run_id, tenant_id, subject_id)
        conversation = self.repository.get_owned(turn.conversation_id, tenant_id, subject_id)
        return turn, conversation

    def _record_completed(
        self,
        run_id: str,
        conversation_id: str,
        final_content: str | None,
    ) -> None:
        if final_content is not None:
            self.repository.append_assistant_message(run_id=run_id, content=final_content)
        self.repository.update_turn_status(run_id, "completed")
        if self.summary_service is not None:
            self.summary_service.build_missing_segments(conversation_id)
            self.summary_service.build_hierarchy(conversation_id)


def _conversation_response(conversation: Any) -> ConversationResponse:
    return ConversationResponse(
        conversation_id=conversation.conversation_id,
        agent_id=conversation.agent_id,
        agent_profile_version=conversation.agent_profile_version,
        status=conversation.status.value,
        created_at=conversation.created_at.isoformat(),
    )


def _server_metadata(context: ExecutionContext, **extra: str) -> dict[str, str]:
    metadata = context.trace_metadata()
    metadata["application_run_id"] = metadata.pop("run_id")
    metadata.update(extra)
    return metadata


def _final_assistant_content(output: Mapping[str, Any]) -> str | None:
    messages = output.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return (
                message.content if isinstance(message.content, str) else json.dumps(message.content)
            )
        if isinstance(message, Mapping) and message.get("type") in {"ai", "assistant"}:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content, default=str)
    return None


def _jsonable_output(output: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if output is None:
        return None
    converted: dict[str, Any] = {}
    for key, value in output.items():
        if isinstance(value, list):
            converted[key] = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in value
            ]
        elif hasattr(value, "model_dump"):
            converted[key] = value.model_dump(mode="json")
        else:
            converted[key] = value
    return converted
