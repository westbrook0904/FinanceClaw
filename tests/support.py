"""Isolated leaf-Agent fixtures; product integration uses the production bootstrap directly."""

from dataclasses import fields
from types import SimpleNamespace

from financeclaw.agent_server.bootstrap import build_components as build_runtime


def build_components(*args, **kwargs):
    """Keep leaf policy/model tests independent of BFF admission and subgraph transport."""
    runtime = build_runtime(*args, **kwargs)
    profile = runtime.default_agent_profile.model_copy(update={"worker_manifest": ()})
    return SimpleNamespace(
        **{field.name: getattr(runtime, field.name) for field in fields(runtime)},
        default_agent_profile=profile,
    )
