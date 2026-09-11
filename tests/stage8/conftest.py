"""Notification fixtures use the Stage 10 product model."""

from types import SimpleNamespace

import pytest

from financeclaw.api.application.conversation_service import ConversationService
from tests.stage10.runtime import SCOPES
from tests.stage10.runtime import runtime as runtime


@pytest.fixture
def setup(runtime):
    """Provide the setup boundary for this test scenario."""
    return SimpleNamespace(
        runtime=runtime,
        settings=runtime.resources.settings,
        store=runtime.turns.store,
        scopes=SCOPES,
        api=ConversationService(
            runtime.turns.journal, runtime.turns.releases.agents, turns=runtime.turns
        ),
        backend=SimpleNamespace(calls=0, questions=False),
    )
