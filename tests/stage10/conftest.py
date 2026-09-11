"""Real application repositories with an explicitly injected native test boundary."""

import pytest

from financeclaw.api.application.turns.bootstrap import build_turns
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


@pytest.fixture
def service(tmp_path):
    """Provide the service boundary for this test scenario."""
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        database_url=f"sqlite+pysqlite:///{tmp_path}/app.db",
        database_auto_create_schema=True,
        artifact_root=str(tmp_path / "artifacts"),
        turn_fallback_seconds=0.1,
    )
    resources = build_resources(settings, enable_persistence=True)
    service = build_turns(settings, resources, client=object())
    yield service
    resources.database.close()


@pytest.fixture
def admit(service):
    """Provide the admit boundary for this test scenario."""

    def create(*, key=None, conversation_id=None, message="hello", **kwargs):
        """Provide the create boundary for this test scenario."""
        if conversation_id is None:
            conversation_id = service.journal.create_conversation(
                tenant_id="tenant",
                subject_id="user",
                agent_id="finance_agent",
                agent_profile_version="1.6.0",
            ).conversation_id
        return service.admission.accept(
            conversation_id,
            ConversationTurnRequest(message=message),
            tenant_id="tenant",
            subject_id="user",
            scopes=frozenset({"*"}),
            idempotency_key=key or conversation_id,
            **kwargs,
        )

    return create
