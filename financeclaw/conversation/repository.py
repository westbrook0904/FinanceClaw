"""Append-only SQLAlchemy Conversation Journal and derived-data repositories."""

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Conversation,
    ConversationMessage,
    ConversationStatus,
    ConversationSummary,
    ConversationTurn,
    MessageRole,
    ModelContextManifest,
    SummaryStatus,
    TurnStatus,
)
from .tables import (
    ConversationMessageRow,
    ConversationRow,
    ConversationSummaryRow,
    ConversationTurnRow,
    ModelContextManifestRow,
)


class ConversationNotFound(LookupError):
    pass


class ConversationConflict(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class ConversationRepository(Protocol):
    def create_conversation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        agent_id: str,
        agent_profile_version: str,
        conversation_id: str | None = None,
        agent_thread_id: str | None = None,
    ) -> Conversation: ...

    def get_owned(self, conversation_id: str, tenant_id: str, subject_id: str) -> Conversation: ...

    def begin_turn(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        message: str,
        target_type: str,
        target_id: str,
        target_version: str,
    ) -> tuple[ConversationTurn, ConversationMessage, bool]: ...

    def bind_server_run(
        self, turn_id: str, server_run_id: str, status: str
    ) -> ConversationTurn: ...

    def get_turn_owned(self, run_id: str, tenant_id: str, subject_id: str) -> ConversationTurn: ...

    def list_messages(
        self, conversation_id: str, *, visible_only: bool = True
    ) -> tuple[ConversationMessage, ...]: ...

    def list_summaries(
        self, conversation_id: str, *, active_only: bool = True
    ) -> tuple[ConversationSummary, ...]: ...

    def get_summary(self, summary_id: str) -> ConversationSummary: ...

    def save_manifest(self, manifest: ModelContextManifest) -> ModelContextManifest: ...


def content_hash(content: str) -> str:
    return sha256(content.encode()).hexdigest()


def _conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        agent_id=row.agent_id,
        agent_profile_version=row.agent_profile_version,
        agent_thread_id=row.agent_thread_id,
        status=ConversationStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _turn(row: ConversationTurnRow) -> ConversationTurn:
    return ConversationTurn(
        turn_id=row.turn_id,
        conversation_id=row.conversation_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        run_id=row.run_id,
        server_run_id=row.server_run_id,
        client_idempotency_key=row.client_idempotency_key,
        request_hash=row.request_hash,
        target_type=row.target_type,
        target_id=row.target_id,
        target_version=row.target_version,
        status=TurnStatus(row.status),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def _message(row: ConversationMessageRow) -> ConversationMessage:
    return ConversationMessage(
        message_id=row.message_id,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        sequence=row.sequence,
        parent_message_id=row.parent_message_id,
        role=MessageRole(row.role),
        content=row.content,
        content_hash=row.content_hash,
        visible=row.visible,
        created_at=row.created_at,
    )


def _summary(row: ConversationSummaryRow) -> ConversationSummary:
    return ConversationSummary(
        summary_id=row.summary_id,
        conversation_id=row.conversation_id,
        level=row.level,
        start_sequence=row.start_sequence,
        end_sequence=row.end_sequence,
        source_message_ids=tuple(row.source_message_ids),
        source_summary_ids=tuple(row.source_summary_ids),
        summary_content=row.summary_content,
        topics=tuple(row.topics),
        entities=tuple(row.entities),
        decisions=tuple(row.decisions),
        open_items=tuple(row.open_items),
        model_profile_version=row.model_profile_version,
        template_version=row.template_version,
        content_hash=row.content_hash,
        status=SummaryStatus(row.status),
        superseded_by=row.superseded_by,
        created_at=row.created_at,
    )


def _manifest(row: ModelContextManifestRow) -> ModelContextManifest:
    return ModelContextManifest.model_validate(
        {
            "manifest_id": row.manifest_id,
            "model_call_id": row.model_call_id,
            "conversation_id": row.conversation_id,
            "turn_id": row.turn_id,
            "run_id": row.run_id,
            "prompt_template_version": row.prompt_template_version,
            "agent_profile_version": row.agent_profile_version,
            "model_profile_version": row.model_profile_version,
            "recent_message_start": row.recent_message_start,
            "recent_message_end": row.recent_message_end,
            "summary_ids": row.summary_ids,
            "memory_ids": row.memory_ids,
            "memory_refs": row.memory_refs,
            "historical_message_ids": row.historical_message_ids,
            "tool_result_refs": row.tool_result_refs,
            "exposed_tools": row.exposed_tools,
            "input_token_count": row.input_token_count,
            "available_input_tokens": row.available_input_tokens,
            "omissions": row.omissions,
            "context_hash": row.context_hash,
            "created_at": row.created_at,
        }
    )


class SqlAlchemyConversationRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create_conversation(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        agent_id: str,
        agent_profile_version: str,
        conversation_id: str | None = None,
        agent_thread_id: str | None = None,
    ) -> Conversation:
        now = datetime.now(UTC)
        resolved_thread_id = agent_thread_id or str(uuid4())
        try:
            resolved_thread_id = str(UUID(resolved_thread_id))
        except ValueError as exc:
            raise ValueError("agent_thread_id must be a UUID") from exc
        row = ConversationRow(
            conversation_id=conversation_id or f"conversation-{uuid4().hex}",
            tenant_id=tenant_id,
            subject_id=subject_id,
            agent_id=agent_id,
            agent_profile_version=agent_profile_version,
            agent_thread_id=resolved_thread_id,
            status=ConversationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            session.add(row)
        return _conversation(row)

    def get_owned(self, conversation_id: str, tenant_id: str, subject_id: str) -> Conversation:
        statement = select(ConversationRow).where(
            ConversationRow.conversation_id == conversation_id,
            ConversationRow.tenant_id == tenant_id,
            ConversationRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ConversationNotFound("conversation was not found for authenticated owner")
            return _conversation(row)

    def begin_turn(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
        message: str,
        target_type: str,
        target_id: str,
        target_version: str,
    ) -> tuple[ConversationTurn, ConversationMessage, bool]:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.tenant_id == tenant_id,
                    ConversationTurnRow.subject_id == subject_id,
                    ConversationTurnRow.client_idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.conversation_id != conversation_id
                    or existing.request_hash != request_hash
                ):
                    raise IdempotencyConflict(
                        "idempotency key was already used for another conversation request"
                    )
                user_message = session.scalar(
                    select(ConversationMessageRow).where(
                        ConversationMessageRow.turn_id == existing.turn_id,
                        ConversationMessageRow.role == MessageRole.USER.value,
                    )
                )
                if user_message is None:
                    raise ConversationConflict("idempotent turn is missing its user message")
                return _turn(existing), _message(user_message), True

            conversation = session.scalar(
                select(ConversationRow)
                .where(
                    ConversationRow.conversation_id == conversation_id,
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.subject_id == subject_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise ConversationNotFound("conversation was not found for authenticated owner")
            if conversation.status != ConversationStatus.ACTIVE.value:
                raise ConversationConflict("conversation is not active")
            max_sequence = session.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.conversation_id == conversation_id
                )
            )
            now = datetime.now(UTC)
            turn_row = ConversationTurnRow(
                turn_id=f"turn-{uuid4().hex}",
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                run_id=f"run-{uuid4().hex}",
                client_idempotency_key=idempotency_key,
                request_hash=request_hash,
                target_type=target_type,
                target_id=target_id,
                target_version=target_version,
                status=TurnStatus.ACCEPTED.value,
                created_at=now,
            )
            message_row = ConversationMessageRow(
                message_id=f"message-{uuid4().hex}",
                conversation_id=conversation_id,
                turn_id=turn_row.turn_id,
                sequence=(max_sequence or 0) + 1,
                role=MessageRole.USER.value,
                content=message,
                content_hash=content_hash(message),
                visible=True,
                created_at=now,
            )
            conversation.updated_at = now
            session.add_all((turn_row, message_row))
        return _turn(turn_row), _message(message_row), False

    def bind_server_run(self, turn_id: str, server_run_id: str, status: str) -> ConversationTurn:
        with self._sessions.begin() as session:
            row = session.get(ConversationTurnRow, turn_id)
            if row is None:
                raise ConversationNotFound("turn was not found")
            if row.server_run_id is not None and row.server_run_id != server_run_id:
                raise ConversationConflict("turn is already bound to another Agent Server run")
            row.server_run_id = server_run_id
            row.status = _normalize_turn_status(status).value
        return _turn(row)

    def get_turn_owned(self, run_id: str, tenant_id: str, subject_id: str) -> ConversationTurn:
        statement = select(ConversationTurnRow).where(
            ConversationTurnRow.run_id == run_id,
            ConversationTurnRow.tenant_id == tenant_id,
            ConversationTurnRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise ConversationNotFound("turn was not found for authenticated owner")
            return _turn(row)

    def update_turn_status(self, run_id: str, status: str) -> ConversationTurn:
        normalized = _normalize_turn_status(status)
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ConversationTurnRow).where(ConversationTurnRow.run_id == run_id)
            )
            if row is None:
                raise ConversationNotFound("turn was not found")
            row.status = normalized.value
            if normalized in {TurnStatus.COMPLETED, TurnStatus.FAILED}:
                row.completed_at = row.completed_at or datetime.now(UTC)
        return _turn(row)

    def append_assistant_message(
        self,
        *,
        run_id: str,
        content: str,
        parent_message_id: str | None = None,
    ) -> ConversationMessage:
        digest = content_hash(content)
        with self._sessions.begin() as session:
            turn = session.scalar(
                select(ConversationTurnRow)
                .where(ConversationTurnRow.run_id == run_id)
                .with_for_update()
            )
            if turn is None:
                raise ConversationNotFound("turn was not found")
            existing = session.scalar(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.turn_id == turn.turn_id,
                    ConversationMessageRow.role == MessageRole.ASSISTANT.value,
                    ConversationMessageRow.parent_message_id == parent_message_id,
                )
            )
            if existing is not None:
                if existing.content_hash != digest:
                    raise ConversationConflict("assistant message reconciliation conflict")
                return _message(existing)
            conversation = session.get(ConversationRow, turn.conversation_id)
            if conversation is None:
                raise ConversationNotFound("conversation was not found")
            max_sequence = session.scalar(
                select(func.max(ConversationMessageRow.sequence)).where(
                    ConversationMessageRow.conversation_id == turn.conversation_id
                )
            )
            now = datetime.now(UTC)
            row = ConversationMessageRow(
                message_id=f"message-{uuid4().hex}",
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                sequence=(max_sequence or 0) + 1,
                parent_message_id=parent_message_id,
                role=MessageRole.ASSISTANT.value,
                content=content,
                content_hash=digest,
                visible=True,
                created_at=now,
            )
            turn.status = TurnStatus.COMPLETED.value
            turn.completed_at = turn.completed_at or now
            conversation.updated_at = now
            session.add(row)
        return _message(row)

    def append_branch_message(
        self,
        *,
        run_id: str,
        content: str,
        parent_message_id: str,
    ) -> ConversationMessage:
        return self.append_assistant_message(
            run_id=run_id, content=content, parent_message_id=parent_message_id
        )

    def list_messages(
        self, conversation_id: str, *, visible_only: bool = True
    ) -> tuple[ConversationMessage, ...]:
        statement: Select[tuple[ConversationMessageRow]] = select(ConversationMessageRow).where(
            ConversationMessageRow.conversation_id == conversation_id
        )
        if visible_only:
            statement = statement.where(ConversationMessageRow.visible.is_(True))
        statement = statement.order_by(ConversationMessageRow.sequence)
        with self._sessions() as session:
            return tuple(_message(row) for row in session.scalars(statement))

    def list_incomplete_turns(self) -> tuple[ConversationTurn, ...]:
        statement = select(ConversationTurnRow).where(
            ConversationTurnRow.status.not_in((TurnStatus.COMPLETED.value, TurnStatus.FAILED.value))
        )
        with self._sessions() as session:
            return tuple(_turn(row) for row in session.scalars(statement))

    def save_summary(
        self, summary: ConversationSummary, *, supersede_ids: Sequence[str] = ()
    ) -> ConversationSummary:
        with self._sessions.begin() as session:
            same_range = session.scalar(
                select(ConversationSummaryRow).where(
                    ConversationSummaryRow.conversation_id == summary.conversation_id,
                    ConversationSummaryRow.level == summary.level,
                    ConversationSummaryRow.start_sequence == summary.start_sequence,
                    ConversationSummaryRow.end_sequence == summary.end_sequence,
                    ConversationSummaryRow.status == SummaryStatus.ACTIVE.value,
                )
            )
            if same_range is not None and same_range.summary_id not in supersede_ids:
                if same_range.content_hash == summary.content_hash:
                    return _summary(same_range)
                raise ConversationConflict("active summary already exists for source range")
            row = ConversationSummaryRow(
                summary_id=summary.summary_id,
                conversation_id=summary.conversation_id,
                level=summary.level,
                start_sequence=summary.start_sequence,
                end_sequence=summary.end_sequence,
                source_message_ids=list(summary.source_message_ids),
                source_summary_ids=list(summary.source_summary_ids),
                summary_content=summary.summary_content,
                topics=list(summary.topics),
                entities=list(summary.entities),
                decisions=list(summary.decisions),
                open_items=list(summary.open_items),
                model_profile_version=summary.model_profile_version,
                template_version=summary.template_version,
                content_hash=summary.content_hash,
                status=SummaryStatus.ACTIVE.value,
                created_at=summary.created_at,
            )
            session.add(row)
            # Materialize the replacement before old rows reference it. This
            # ordering is required when SQLite foreign-key checks are enabled.
            session.flush()
            for summary_id in supersede_ids:
                old = session.get(ConversationSummaryRow, summary_id)
                if old is None or old.conversation_id != summary.conversation_id:
                    raise ConversationConflict("summary rebuild source does not match conversation")
                old.status = SummaryStatus.SUPERSEDED.value
                old.superseded_by = summary.summary_id
        return summary

    def list_summaries(
        self, conversation_id: str, *, active_only: bool = True
    ) -> tuple[ConversationSummary, ...]:
        statement = select(ConversationSummaryRow).where(
            ConversationSummaryRow.conversation_id == conversation_id
        )
        if active_only:
            statement = statement.where(ConversationSummaryRow.status == SummaryStatus.ACTIVE.value)
        statement = statement.order_by(
            ConversationSummaryRow.level,
            ConversationSummaryRow.start_sequence,
        )
        with self._sessions() as session:
            return tuple(_summary(row) for row in session.scalars(statement))

    def get_summary(self, summary_id: str) -> ConversationSummary:
        with self._sessions() as session:
            row = session.get(ConversationSummaryRow, summary_id)
            if row is None:
                raise ConversationNotFound("summary was not found")
            return _summary(row)

    def save_manifest(self, manifest: ModelContextManifest) -> ModelContextManifest:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ModelContextManifestRow).where(
                    ModelContextManifestRow.model_call_id == manifest.model_call_id
                )
            )
            if existing is not None:
                if existing.context_hash != manifest.context_hash:
                    raise ConversationConflict("model_call_id has conflicting context manifest")
                return _manifest(existing)
            row = ModelContextManifestRow(
                manifest_id=manifest.manifest_id,
                model_call_id=manifest.model_call_id,
                conversation_id=manifest.conversation_id,
                turn_id=manifest.turn_id,
                run_id=manifest.run_id,
                prompt_template_version=manifest.prompt_template_version,
                agent_profile_version=manifest.agent_profile_version,
                model_profile_version=manifest.model_profile_version,
                recent_message_start=manifest.recent_message_start,
                recent_message_end=manifest.recent_message_end,
                summary_ids=list(manifest.summary_ids),
                memory_ids=list(manifest.memory_ids),
                memory_refs=[item.model_dump(mode="json") for item in manifest.memory_refs],
                historical_message_ids=list(manifest.historical_message_ids),
                tool_result_refs=list(manifest.tool_result_refs),
                exposed_tools=list(manifest.exposed_tools),
                input_token_count=manifest.input_token_count,
                available_input_tokens=manifest.available_input_tokens,
                omissions=[item.model_dump(mode="json") for item in manifest.omissions],
                context_hash=manifest.context_hash,
                created_at=manifest.created_at,
            )
            session.add(row)
        return manifest

    def list_manifests(self, conversation_id: str) -> tuple[ModelContextManifest, ...]:
        statement = (
            select(ModelContextManifestRow)
            .where(ModelContextManifestRow.conversation_id == conversation_id)
            .order_by(ModelContextManifestRow.created_at)
        )
        with self._sessions() as session:
            return tuple(_manifest(row) for row in session.scalars(statement))


def _normalize_turn_status(status: str) -> TurnStatus:
    mapping = {
        "accepted": TurnStatus.ACCEPTED,
        "pending": TurnStatus.PENDING,
        "running": TurnStatus.RUNNING,
        "waiting_child": TurnStatus.WAITING_CHILD,
        "interrupted": TurnStatus.INTERRUPTED,
        "success": TurnStatus.COMPLETED,
        "completed": TurnStatus.COMPLETED,
        "error": TurnStatus.FAILED,
        "failed": TurnStatus.FAILED,
    }
    return mapping.get(status, TurnStatus.PENDING)
