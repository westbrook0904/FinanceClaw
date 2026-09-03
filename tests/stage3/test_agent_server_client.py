from types import SimpleNamespace

import pytest

from financeclaw.application.agent_server_client import LangGraphAgentServerClient


class _Runs:
    async def get(self, thread_id: str, run_id: str) -> dict[str, str]:
        del thread_id
        return {"run_id": run_id, "status": "success"}


class _Threads:
    async def get_state(self, thread_id: str) -> dict[str, object]:
        del thread_id
        return {
            "metadata": {"run_id": "server-run"},
            "interrupts": [{"value": {"action_requests": [{"name": "confirm_memory"}]}}],
        }


@pytest.mark.asyncio
async def test_successful_server_run_at_hitl_checkpoint_is_normalized() -> None:
    """Agent Server 0.13 stores HITL state on the thread, not run status."""

    client = object.__new__(LangGraphAgentServerClient)
    client._client = SimpleNamespace(runs=_Runs(), threads=_Threads())

    result = await client.get_run(thread_id="thread", run_id="server-run")

    assert result["status"] == "interrupted"
