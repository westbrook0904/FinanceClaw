"""One resource set per server process, shared by custom HTTP lifespan and graph factories."""

from functools import lru_cache

from financeclaw.shared.infrastructure.observability.langsmith import configure_langsmith
from financeclaw.shared.infrastructure.observability.telemetry import configure_telemetry
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings

_telemetry = None


def configure_observability(settings):
    """Configure role telemetry and privacy before constructing graph or client resources."""
    configure_langsmith(
        project=settings.langsmith_project,
        endpoint=settings.langsmith_endpoint,
        sample_rate=settings.langsmith_trace_sample_rate,
        hide_inputs=settings.langsmith_hide_inputs,
        hide_outputs=settings.langsmith_hide_outputs,
    )
    return configure_telemetry(
        service_name="financeclaw-" + settings.process_role,
        environment=settings.environment.value,
        endpoint=settings.otel_exporter_endpoint,
        metrics_endpoint=settings.otel_metrics_exporter_endpoint,
        sample_rate=settings.otel_trace_sample_rate,
    )


@lru_cache(maxsize=1)
def process_resources():
    """Construct one shared application resource set for the native server process."""
    global _telemetry
    settings = FinanceClawSettings()
    _telemetry = configure_observability(settings)
    return build_resources(settings, enable_persistence=True)


def close_process_resources():
    """Close the process database only after graph and API owners have drained."""
    global _telemetry
    if _telemetry is not None:
        _telemetry.shutdown()
        _telemetry = None
    if process_resources.cache_info().currsize:
        process_resources().database.close()
        process_resources.cache_clear()
