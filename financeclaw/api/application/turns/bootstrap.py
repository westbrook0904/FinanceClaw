"""Assemble the product application without compiling graphs or opening remote HTTP clients."""

from langgraph_sdk import get_client

from financeclaw.api.application.turns.backend import NativeRuns
from financeclaw.api.application.turns.lifecycle import TurnLifecycle
from financeclaw.api.application.turns.progress import TurnEvents
from financeclaw.api.application.turns.releases import TurnReleases
from financeclaw.api.application.turns.service import TurnService
from financeclaw.shared.releases.catalog import build_release_catalogs
from financeclaw.shared.turns.repository import TurnRepository


def build_turns(settings, resources, *, client=None):
    """Assemble admission and observation around the process-local native SDK client."""
    catalogs = build_release_catalogs(settings, enable_persistence=True)
    store = TurnRepository(resources.database.session_factory)
    store.require_schema()
    service = TurnService(
        store, resources.conversation_repository, TurnReleases(catalogs), settings
    )
    native = NativeRuns(
        client if client is not None else get_client(url=None, api_key=None),
        service.releases,
        service.execution,
    )
    from financeclaw.api.application.maintenance import CheckpointMaintenance
    from financeclaw.shared.conversation.lifecycle import ConversationRetention

    service.maintenance = CheckpointMaintenance(
        ConversationRetention(resources.database.session_factory, resources.artifact_service.store),
        native.client,
    )
    TurnLifecycle(service, native)
    TurnEvents(service)
    return service
