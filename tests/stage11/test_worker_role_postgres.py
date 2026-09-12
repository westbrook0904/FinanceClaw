"""Verify the least-privilege identity only inside a new disposable PostgreSQL database."""

import os
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url

from deploy.provision_memory_role import provision, verify_role
from financeclaw.kernel.models import ModelProfile
from financeclaw.memory_worker.bootstrap import require_schema
from financeclaw.memory_worker.consolidation import ConsolidationHandler
from financeclaw.memory_worker.extraction import ExtractionHandler
from financeclaw.memory_worker.model import OfflineMemoryModel, StructuredMemoryModel
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase, normalize_database_url
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.models import MemoryActor
from financeclaw.shared.memory.repository import content_hash
from financeclaw.shared.memory.tables import MemoryOwnerRow, MemoryRecordRow
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow
from tests.stage3.support import conversation_context
from tests.stage11.test_worker_pipeline import suggestions


def connect(url, **options):
    """Pass credentials as connection parameters, never command arguments or test output."""
    return psycopg.connect(
        dbname=url.database,
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        **dict(url.query),
        **options,
    )


@pytest.fixture
def restricted_database():
    """Revoke PUBLIC grants only in a new database; remove that database and its new role."""
    raw = os.getenv("FINANCECLAW_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("FINANCECLAW_TEST_POSTGRES_URL is required for PostgreSQL role verification")
    url = make_url(normalize_database_url(raw)).set(query={})
    identifier = uuid4().hex
    database_name = "stage11_role_db_" + identifier
    role_name = "stage11_role_" + identifier
    password = secrets.token_urlsafe(32)
    admin_database = None
    memory_database = None
    with connect(url, autocommit=True) as cluster:
        cluster.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        scoped_url = url.set(database=database_name)
        try:
            admin_database = ApplicationDatabase(scoped_url.render_as_string(hide_password=False))
            admin_database.initialize_schema()
            with connect(scoped_url) as admin:
                assert provision(admin, password=password, role_name=role_name)
            memory_url = scoped_url.set(username=role_name, password=password)
            memory_database = ApplicationDatabase(memory_url.render_as_string(hide_password=False))
            yield admin_database, memory_database, scoped_url, role_name, password
        finally:
            if memory_database:
                memory_database.close()
            if admin_database:
                admin_database.close()
            cluster.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )
            cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name)))


def test_role_verification_is_idempotent_and_denies_business_write_and_ddl(restricted_database):
    """Verify grants without upgrading; deny business mutation, audit rewriting and DDL."""
    _, memory, url, role_name, password = restricted_database
    require_schema(memory)
    with connect(url) as admin:
        assert not provision(admin, password=password, role_name=role_name)
    with connect(url.set(username=role_name, password=password), autocommit=True) as connection:
        assert connection.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 0
        for command in (
            "UPDATE conversation_messages SET content = content WHERE false",
            "DELETE FROM conversation_turns WHERE false",
            "UPDATE audit_records SET event_type = event_type WHERE false",
            "DELETE FROM notification_events WHERE false",
            "TRUNCATE memory_records",
            "SELECT * FROM artifacts LIMIT 0",
            "CREATE TABLE forbidden_table (id integer)",
            "CREATE TEMP TABLE forbidden_temporary (id integer)",
            "CREATE SCHEMA forbidden_schema",
            "ALTER TABLE memory_records ADD COLUMN forbidden integer",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(command)
    with connect(url) as admin:
        admin.execute(
            sql.SQL("GRANT UPDATE ON conversation_messages TO {}").format(sql.Identifier(role_name))
        )
    with connect(url) as admin:
        with pytest.raises(RuntimeError, match="grant mismatch"):
            verify_role(admin, role_name=role_name)


@pytest.mark.asyncio
async def test_restricted_role_runs_the_complete_memory_pipeline(restricted_database):
    """Read Journal with real role credentials and commit memory, audit and index jobs."""
    admin, memory, _, _, _ = restricted_database
    conversations = SqlAlchemyConversationRepository(admin.session_factory)
    context, message_id = conversation_context(
        conversations, message="研究长期债券的风险和期限影响"
    )
    actor = MemoryActor(
        tenant_id=context.tenant_id,
        subject_id=context.subject_id,
        turn_id=context.turn_id,
        conversation_id=context.conversation_id,
        scopes=context.scopes,
    )
    outbox = SqlAlchemyOutboxRepository(memory.session_factory)
    profile = ModelProfile(
        profile_id="role-probe",
        version="1.0.0",
        model="offline",
        max_tokens=2000,
        context_window_tokens=64000,
        token_estimator="utf8-bytes-v1",
    )
    offline = OfflineMemoryModel()
    model = StructuredMemoryModel(offline, profile, outbox)
    intake = MemoryIntake(admin.session_factory)
    with admin.session_factory.begin() as session:
        source = intake.register_message_in_session(session, actor, message_id)
        session.get(ConversationTurnRow, actor.turn_id).status = "completed"
        session.add(
            ConversationMessageRow(
                message_id="role-final",
                turn_id=actor.turn_id,
                conversation_id=actor.conversation_id,
                sequence=2,
                role="assistant",
                content="已完成债券久期研究，说明了风险。",
                content_hash=content_hash("已完成债券久期研究，说明了风险。"),
            )
        )
        session.flush()
        intake.close_turn_in_session(
            session, actor, actor.turn_id, model_profile_version=model.fingerprint
        )
    extraction = ExtractionHandler(
        memory.session_factory, outbox, model, consolidation_model_version=model.fingerprint
    )
    await extraction.process(outbox.claim_pending(destination="memory_extract", limit=1)[0])
    output = suggestions(source, actor)
    offline.outputs.append(output)
    await extraction.process(outbox.claim_pending(destination="memory_extract", limit=1)[0])
    with memory.session_factory.begin() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        session.get(
            OutboxEventRow, owner.active_consolidation_event_id
        ).available_at = datetime.now(UTC)
    offline.outputs.append(output)
    consolidation = ConsolidationHandler(memory.session_factory, outbox, model)
    await consolidation.process(outbox.claim_pending(destination="memory_consolidate", limit=1)[0])
    with memory.session_factory() as session:
        record = session.scalar(select(MemoryRecordRow).where(MemoryRecordRow.is_current.is_(True)))
        assert record.status == "active" and record.kind == "task"
    assert len(outbox.claim_pending(destination="memory_index", limit=5)) == 1
