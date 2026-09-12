"""Run memory serialization against independent real PostgreSQL connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select

from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.models import MemoryActor, MemoryConflict, MemoryMutation
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import MemoryRepository
from financeclaw.shared.memory.tables import MemoryRecordRow, MemorySourceRow
from tests.stage11.domain_support import seed_user_journal
from tests.stage11.test_worker_postgres import postgres_database as _postgres_database

postgres_database = _postgres_database


def test_concurrent_profile_corrections_use_owner_lock_and_expected_revision(postgres_database):
    """S12: only one independently connected correction may replace a reviewed revision."""
    sessions = postgres_database.session_factory
    service = MemoryMutationService(sessions)
    actor = MemoryActor(tenant_id="pg-tenant", subject_id="pg-owner", scopes={"*"})
    original = service.apply(
        actor,
        MemoryMutation(
            mutation_id="original",
            kind="profile",
            field="language",
            content="zh-CN",
            explicit_intent=True,
        ),
    )
    start = Barrier(2)

    def correct(index):
        """Start separate service transactions together without blocking inside SQL locks."""
        start.wait(timeout=5)
        try:
            return service.apply(
                actor,
                MemoryMutation(
                    mutation_id=f"correction-{index}",
                    operation="update",
                    memory_id=original.memory_id,
                    expected_revision=1,
                    kind="profile",
                    field="language",
                    content="en",
                    explicit_intent=True,
                ),
            )
        except MemoryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(correct, (1, 2)))
    assert sum(result is not None for result in results) == 1
    assert MemoryRepository(sessions).get(actor, original.memory_id).revision == 2
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditRecordRow)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(MemoryRecordRow)
                .where(MemoryRecordRow.is_current.is_(True))
            )
            == 1
        )


def test_concurrent_source_admission_assigns_unique_owner_order(postgres_database):
    """Source chronology is serialized across separate conversations before async work begins."""
    sessions = postgres_database.session_factory
    conversations = SqlAlchemyConversationRepository(sessions)
    actor = MemoryActor(
        tenant_id="tenant.a",
        subject_id="subject.a",
        scopes={"memory:read", "memory:write", "memory:delete"},
    )
    entries = [
        seed_user_journal(conversations, actor, f"研究计划 {index}", f"source-{index}")
        for index in (1, 2)
    ]
    start = Barrier(2)

    def register(entry):
        """Register sources on separate database connections at the same admission boundary."""
        context, message_id = entry
        actor = MemoryActor(
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            scopes=context.scopes,
            turn_id=context.turn_id,
            conversation_id=context.conversation_id,
        )
        start.wait(timeout=5)
        with sessions.begin() as session:
            return MemoryIntake(sessions).register_message_in_session(session, actor, message_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        sources = list(executor.map(register, entries))
    assert sorted(source.source_seq for source in sources) == [1, 2]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(MemorySourceRow)) == 2


def test_permanent_receipt_keeps_new_profile_after_connection_reconstruction(postgres_database):
    """S13: replaying an old operation across new sessions cannot rewrite the current head."""
    sessions = postgres_database.session_factory
    actor = MemoryActor(tenant_id="pg-tenant", subject_id="pg-owner", scopes={"*"})
    original = MemoryMutation(
        mutation_id="old", kind="profile", field="language", content="zh-CN", explicit_intent=True
    )
    first = MemoryMutationService(sessions).apply(actor, original)
    MemoryMutationService(sessions).apply(
        actor,
        MemoryMutation(
            mutation_id="new",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=1,
            kind="profile",
            field="language",
            content="en",
            explicit_intent=True,
        ),
    )
    replay = MemoryMutationService(sessions).apply(actor, original)
    assert replay.replayed and replay.revision == 1
    assert MemoryRepository(sessions).get(actor, first.memory_id).content == "en"
