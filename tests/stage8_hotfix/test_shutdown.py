"""BFF shutdown releases even a claim committed during cancellation."""

import asyncio
import threading

import pytest

from financeclaw.shared.execution_ledger.run_tables import RootRunRow
from tests.stage8_hotfix.test_bff_runs import admit, tick
from tests.stage8_hotfix.test_bff_runs import runtime as runtime


@pytest.mark.asyncio
async def test_shutdown_during_sql_claim_releases_the_committed_lease(runtime, monkeypatch):
    """Cancellation after SQL commits but before to_thread returns cannot strand a lease."""
    accepted = await admit(runtime)
    original = runtime.runs.store.claim_due
    committed, release = threading.Event(), threading.Event()

    def delayed_claim(*args, **kwargs):
        """Hold only the return path after the real SQL claim has committed."""
        claim = original(*args, **kwargs)
        committed.set()
        assert release.wait(10)
        return claim

    monkeypatch.setattr(runtime.runs.store, "claim_due", delayed_claim)
    task = asyncio.create_task(runtime.lifecycle.tick())
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    with runtime.runs.store.sessions() as session:
        root = session.get(RootRunRow, accepted.run_id)
        assert root.owner is None and root.lease_until is None
        assert not runtime.client.runs.calls
    monkeypatch.setattr(runtime.runs.store, "claim_due", original)
    await tick(runtime)
    assert len(runtime.client.runs.calls) == 1
