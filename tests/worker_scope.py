"""Test-only root admission for focused Worker graph tests."""

from contextlib import contextmanager

from financeclaw.agent_server.tools.subgraph_scope import InvocationScope, active_scope
from financeclaw.kernel.agents import AgentProfile
from financeclaw.shared.releases.subgraphs import worker_declaration
from financeclaw.shared.turns.snapshots import agent_snapshot
from tests.turn_support import seed_execution


def worker_snapshot(profile, context, catalog, models, *, thread_id="test-root", input_hash="test"):
    """Pin a Worker under a synthetic root with real schema and authorization checks."""
    context = context.model_copy(update={"turn_id": context.turn_id})
    declaration = worker_declaration(profile, catalog, models)
    root = AgentProfile(
        agent_id="test_orchestrator",
        version="1.0.0",
        memory_policy="none",
        system_prompt_template="Synthetic test root",
        allowed_tools=(),
        worker_manifest=(declaration,),
        model_profile=getattr(profile, "model_profile", {"profile_id": "test", "version": "1.0.0"}),
    )
    return agent_snapshot(root, context, thread_id=thread_id, input_hash=input_hash)


@contextmanager
def invocation(execution, profile, catalog, models, context):
    """Enter the same server-owned scope as a composite Tool while exposing graph state to tests."""
    context = context.model_copy(update={"turn_id": context.turn_id})
    declaration = worker_declaration(profile, catalog, models)
    context = seed_execution(execution, context, worker_snapshot(profile, context, catalog, models))
    token = active_scope.set(InvocationScope(declaration, context, "test-tool-call", "b" * 64))
    try:
        yield context
    finally:
        active_scope.reset(token)
