"""隔离 SQLite Journal、Artifact 与原生内存 Store 的测试基础。"""

import pytest
from langgraph.store.memory import InMemoryStore

from financeclaw.agent_server.memory.service import LongTermMemoryService
from financeclaw.shared.artifacts.repository import SqlAlchemyArtifactRepository
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.artifacts.storage import LocalArtifactStore
from financeclaw.shared.audit.repository import InMemoryAuditRepository
from tests.stage3.support import conversation_context, journal


@pytest.fixture
def memory_stack(tmp_path):
    """真实仓储与只在本测试存活的原生 Store。"""
    database, repository = journal(tmp_path / "stage9.db")
    context, message_id = conversation_context(repository, message="以后都用中文、回答简短些")
    artifacts = ArtifactService(
        SqlAlchemyArtifactRepository(database.session_factory),
        LocalArtifactStore(str(tmp_path / "artifacts")),
        inline_bytes=512,
    )
    service = LongTermMemoryService(
        conversation_repository=repository, audit=InMemoryAuditRepository()
    )
    yield context, message_id, repository, artifacts, service, InMemoryStore()
    database.close()
