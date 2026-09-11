"""Explicit checkpoint maintenance through the same private native SDK transport."""

from financeclaw.shared.infrastructure.asyncio import run_sync


class CheckpointMaintenance:
    """Prune archived owned conversations with short database transactions."""

    def __init__(self, retention, client):
        """Inject business eligibility rules and the API's process-local client."""
        self.retention, self.client = retention, client

    async def prune(
        self, *, conversation_id, tenant_id, subject_id, apply=False, strategy="keep_latest"
    ):
        """Verify native idleness after business eligibility; preview unless explicitly applied."""
        if strategy not in {"keep_latest", "delete"}:
            raise ValueError("unsupported checkpoint strategy")
        identity = dict(conversation_id=conversation_id, tenant_id=tenant_id, subject_id=subject_id)
        threads = await run_sync(self.retention.checkpoint_candidates, **identity)
        for identifier in threads:
            thread = await self.client.threads.get(identifier)
            state = await self.client.threads.get_state(identifier, subgraphs=True)
            if (
                thread.get("status") != "idle"
                or state.get("next")
                or state.get("interrupts")
                or any(
                    task.get("interrupts") or task.get("error") for task in state.get("tasks", [])
                )
            ):
                raise ValueError("native thread still has pending work")
        result = {"threads": threads, "strategy": strategy, "applied": False}
        if apply:
            if threads != await run_sync(self.retention.checkpoint_candidates, **identity):
                raise ValueError("checkpoint retention candidates changed")
            result["native_result"] = await self.client.threads.prune(threads, strategy=strategy)
            result["applied"] = True
        return result
