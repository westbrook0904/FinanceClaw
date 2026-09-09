"""Shared notification fixtures with BFF-owned root execution."""

from types import SimpleNamespace

import pytest

from financeclaw.bff.application.conversation_service import ConversationService
from tests.stage8_hotfix.test_bff_runs import SCOPES
from tests.stage8_hotfix.test_bff_runs import runtime as runtime


@pytest.fixture
def setup(runtime):
    """Expose notification dependencies while keeping execution entirely in BFF."""
    return SimpleNamespace(
        runtime=runtime,
        settings=runtime.resources.settings,
        store=runtime.runs.store,
        scopes=SCOPES,
        bff=ConversationService(
            runtime.runs.repository, runtime.releases.agent_profiles, runs=runtime.runs
        ),
        backend=SimpleNamespace(calls=0, questions=False),
    )
