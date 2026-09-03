"""Central LangSmith project, sampling and privacy configuration."""

import os

import langsmith


def configure_langsmith(
    *,
    project: str,
    endpoint: str,
    sample_rate: float,
    hide_inputs: bool,
    hide_outputs: bool,
) -> None:
    """Apply one environment-specific policy before any request is executed."""

    os.environ["LANGSMITH_PROJECT"] = project
    os.environ["LANGSMITH_ENDPOINT"] = endpoint
    os.environ["LANGSMITH_TRACING_SAMPLING_RATE"] = str(sample_rate)
    os.environ["LANGSMITH_HIDE_INPUTS"] = str(hide_inputs).lower()
    os.environ["LANGSMITH_HIDE_OUTPUTS"] = str(hide_outputs).lower()
    langsmith.configure(project_name=project)
