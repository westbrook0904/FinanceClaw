"""装配 BFF 根运行控制与持久化资源。"""

from dataclasses import dataclass

from financeclaw.bff.application.runs.backend import NativeRunCommands, NativeRunReader
from financeclaw.bff.application.runs.commands import RunCommandService, RunObserver
from financeclaw.bff.application.runs.lifecycle import BFFRunLifecycle
from financeclaw.bff.application.runs.releases import BFFReleases
from financeclaw.bff.application.runs.results import ResultService
from financeclaw.bff.application.runs.service import BFFRunService
from financeclaw.bff.application.runs.store import BFFRunRepository
from financeclaw.shared.backends.langgraph import LangGraphAgentServerClient
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.releases.catalog import build_release_catalogs


@dataclass(frozen=True)
class BFFRunServices:
    """Shared BFF resources plus application-owned admission, commands and observation."""

    resources: object
    releases: object
    client: object
    runs: BFFRunService
    lifecycle: BFFRunLifecycle


def build_bff_runs(settings, *, resources=None, catalogs=None, native=None):
    """Assemble the current root release, native backend and durable lifecycle."""
    resources = resources or build_resources(settings, enable_persistence=True)
    catalogs = catalogs or build_release_catalogs(settings, enable_persistence=True)
    store = BFFRunRepository(
        resources.database.session_factory,
        backend_instance_id=settings.bff_backend_instance_id,
        journal=resources.conversation_repository,
    )
    store.require_schema()
    native = native or LangGraphAgentServerClient(
        url=settings.agent_server_url,
        service_token=settings.agent_server_service_token.get_secret_value()
        if settings.agent_server_service_token
        else None,
        timeout_seconds=settings.agent_server_timeout_seconds,
    )
    releases = BFFReleases(catalogs)
    service = BFFRunService(store, releases, settings)
    reader = NativeRunReader(native, store)
    results = ResultService(service)
    commands = RunCommandService(
        store, NativeRunCommands(reader, releases, settings), reader, results
    )
    lifecycle = BFFRunLifecycle(service, commands, RunObserver(store, reader, results))
    return BFFRunServices(resources, catalogs, native, service, lifecycle)
