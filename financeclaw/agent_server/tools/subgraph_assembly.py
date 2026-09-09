"""Build leaf capabilities, then Worker graphs, then Tools; no recursive bootstrap."""

import json

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.agent_server.graphs.workflows.portfolio_review_v1 import (
    build_portfolio_review_graph,
)
from financeclaw.agent_server.graphs.ziwei_agent import build_ziwei_agent
from financeclaw.agent_server.tools.subgraphs import subgraph_tool


def assemble_subgraph_tools(releases, factory, *, settings, ziwei_service=None, models=None):
    """Compile each reachable Worker once with inherited per-invocation persistence."""
    models = models or {}
    result = []
    execution = getattr(factory.conversation_repository, "execution", None)
    for declaration in releases.agent_profiles.resolve("finance_agent", "1.5.0").worker_manifest:
        entry = json.loads(declaration)
        if entry["kind"] == "agent":
            release = releases.agent_profiles.resolve(entry["target_id"], entry["version"])
            model = models.get(release.agent_id)
            if release.agent_id == "ziwei_doushu_agent":
                graph = build_ziwei_agent(
                    factory,
                    release,
                    ziwei_service,
                    model=model or (OfflineZiweiModel() if settings.offline_model else None),
                    checkpointer=None,
                    input_budget=settings.context_input_limit - settings.context_reserved_output,
                )
            else:
                graph = factory.build(
                    release,
                    model=model or (OfflineFinanceModel() if settings.offline_model else None),
                    checkpointer=None,
                )
        else:
            release = releases.workflow_catalog.resolve(entry["target_id"], entry["version"])
            graph = build_portfolio_review_graph(
                catalog=factory.tool_catalog,
                policy=factory.tool_policy,
                audit=factory.audit,
                artifact_service=factory.artifact_service,
                release=release,
                resource_gate=factory.resource_gate,
                execution=execution,
                read_max_attempts=settings.read_max_attempts,
                checkpointer=None,
            )
        result.append(
            subgraph_tool(
                release,
                graph,
                declaration,
                execution=execution,
                conversations=factory.conversation_repository,
                artifacts=factory.artifact_service,
            )
        )
    return tuple(result)
