"""Notification tests driven by BFF and an SDK-shaped native transport."""

from types import SimpleNamespace

from sqlalchemy import select

from financeclaw.shared.execution_ledger.run_tables import RootRunRow
from tests.stage8_hotfix.test_bff_runs import final_state, waiting_state
from tests.stage8_hotfix.test_bff_runs import tick as bff_tick


async def tick(setup):
    """Produce evidence only for an already submitted native attempt, then run BFF recovery."""
    runtime = setup.runtime
    with setup.store.sessions() as session:
        roots = list(session.scalars(select(RootRunRow)))
    for root in roots:
        if not runtime.runs.execution.get(root.run_id)["server_run_id"] or not root.active:
            continue
        accepted = SimpleNamespace(run_id=root.run_id, thread_id=root.projection["thread_id"])
        if setup.backend.questions and len(runtime.client.runs.calls) == 1:
            waiting_state(runtime, accepted)
        else:
            final_state(runtime, accepted, content="final answer")
    await bff_tick(runtime)
    setup.backend.calls = len(runtime.client.runs.calls)
