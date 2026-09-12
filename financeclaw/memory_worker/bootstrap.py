"""Build only application SQL, structured model clients and memory job handlers."""

from dataclasses import dataclass

from sqlalchemy import inspect

from financeclaw.kernel.models import ModelProfileCatalog, ModelProfileRef
from financeclaw.memory_worker.consolidation import CONSOLIDATION_DESTINATION, ConsolidationHandler
from financeclaw.memory_worker.extraction import EXTRACTION_DESTINATION, ExtractionHandler
from financeclaw.memory_worker.model import OfflineMemoryModel, StructuredMemoryModel
from financeclaw.memory_worker.runner import MemoryJobRunner
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.security.egress import EgressPolicy
from financeclaw.shared.llm.factory import ModelFactory
from financeclaw.shared.llm.memory_profiles import memory_model_profiles
from financeclaw.shared.memory.tables import (
    MemoryExtractionRow,
    MemoryOwnerRow,
    MemoryRecordRow,
    MemorySourceRow,
)
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow


@dataclass(frozen=True)
class MemoryWorkerResources:
    """Resources owned by this process; no graph factory, HTTP app or channel connector."""

    database: ApplicationDatabase
    extraction: MemoryJobRunner
    consolidation: MemoryJobRunner


def build_memory_worker(settings) -> MemoryWorkerResources:
    """Keep both worker purposes separately bounded and freeze their complete profiles."""
    if settings.process_role != "memory_worker":
        raise ValueError("memory worker process_role is required")
    if not settings.offline_model and settings.provider_base_url:
        EgressPolicy(
            settings.egress_allowed_hosts,
            require_https=settings.environment.value in {"staging", "production"},
        ).validate(settings.provider_base_url)
    profiles = memory_model_profiles(settings)
    enabled = settings.memory_enabled and settings.memory_auto_extract
    factory = ModelFactory(
        ModelProfileCatalog(profiles),
        api_key=settings.provider_api_key,
        base_url=settings.provider_base_url,
    )
    database = ApplicationDatabase(
        settings.database_url.get_secret_value(),
        statement_timeout_seconds=settings.database_statement_timeout_seconds,
    )
    try:
        if settings.database_auto_create_schema:
            database.initialize_schema()
        require_schema(database)
        outbox = SqlAlchemyOutboxRepository(database.session_factory)
        models = [
            StructuredMemoryModel(
                OfflineMemoryModel()
                if settings.offline_model or not enabled
                else factory.create(
                    ModelProfileRef(profile_id=profile.profile_id, version=profile.version)
                ),
                profile,
                outbox,
                timeout_seconds=settings.memory_model_timeout_seconds,
            )
            for profile in profiles
        ]
        extraction = ExtractionHandler(
            database.session_factory,
            outbox,
            models[0],
            consolidation_model_version=models[1].fingerprint,
            enabled=enabled,
        )
        consolidation = ConsolidationHandler(
            database.session_factory,
            outbox,
            models[1],
            enabled=enabled,
            candidate_seconds=settings.memory_candidate_seconds,
            auto_commit_low_risk=settings.memory_auto_commit_low_risk_preferences,
        )
        options = dict(
            lease_seconds=settings.memory_worker_lease_seconds,
            renew_seconds=settings.memory_worker_renew_seconds,
            poll_seconds=settings.memory_worker_poll_seconds,
        )
        return MemoryWorkerResources(
            database,
            MemoryJobRunner(
                outbox,
                extraction,
                destination=EXTRACTION_DESTINATION,
                concurrency=settings.memory_extraction_concurrency,
                **options,
            ),
            MemoryJobRunner(
                outbox,
                consolidation,
                destination=CONSOLIDATION_DESTINATION,
                concurrency=settings.memory_consolidation_concurrency,
                **options,
            ),
        )
    except BaseException:
        database.close()
        raise


def require_schema(database):
    """Reject absent or stale memory/Outbox schema before declaring the consumer healthy."""
    schema = inspect(database.engine)
    for table in (
        MemoryOwnerRow,
        MemorySourceRow,
        MemoryExtractionRow,
        MemoryRecordRow,
        OutboxEventRow,
    ):
        if not schema.has_table(table.__tablename__) or not set(
            table.__table__.columns.keys()
        ).issubset(column["name"] for column in schema.get_columns(table.__tablename__)):
            raise RuntimeError("Stage 11 application memory schema is required")
