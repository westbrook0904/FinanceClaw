"""Test-only root admission for focused Worker graph tests."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from financeclaw.agent_server.tools.subgraph_scope import InvocationScope, active_scope
from financeclaw.kernel.agents import AgentProfile
from financeclaw.shared.execution_ledger.run_tables import RunAuthorizationRow
from financeclaw.shared.execution_ledger.snapshots import agent_snapshot
from financeclaw.shared.execution_ledger.tables import RunExecutionRow
from financeclaw.shared.releases.subgraphs import worker_declaration


def worker_snapshot(profile, context, catalog, models, *, thread_id="test-root", input_hash="test"):
    """Pin a Worker under a synthetic root with real schema and authorization checks."""
    context = context.model_copy(update={"root_run_id": context.run_id})
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
    context = context.model_copy(update={"root_run_id": context.run_id})
    declaration = worker_declaration(profile, catalog, models)
    with execution.sessions() as session:
        exists = session.get(RunExecutionRow, context.run_id) is not None
    if not exists:
        execution.register(context.run_id, worker_snapshot(profile, context, catalog, models))
    with execution.sessions.begin() as session:
        if session.get(RunAuthorizationRow, context.run_id) is None:
            session.add(
                RunAuthorizationRow(
                    run_id=context.run_id,
                    scopes=sorted(context.scopes),
                    source="test",
                    source_hash="a" * 64,
                    issued_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    token = active_scope.set(InvocationScope(declaration, context, "test-tool-call", "b" * 64))
    try:
        yield context
    finally:
        active_scope.reset(token)
