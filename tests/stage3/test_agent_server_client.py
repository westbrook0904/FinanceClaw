"""`test_agent_server_client` 模块提供`stage3`相关能力。"""

from types import SimpleNamespace

import pytest

from financeclaw.infrastructure.clients.agent_server import LangGraphAgentServerClient


class _Runs:
    """`_Runs` 封装该模块内聚的状态与行为。"""

    async def get(self, thread_id: str, run_id: str) -> dict[str, str]:
        """获取 `_Runs`，并返回边界约定的结果。"""
        del thread_id
        return {"run_id": run_id, "status": "success"}


class _Threads:
    """`_Threads` 封装该模块内聚的状态与行为。"""

    async def get_state(self, thread_id: str) -> dict[str, object]:
        """获取 `state`，并返回边界约定的结果。"""
        del thread_id
        return {
            "metadata": {"run_id": "server-run"},
            "interrupts": [{"value": {"action_requests": [{"name": "confirm_memory"}]}}],
        }


@pytest.mark.asyncio
async def test_successful_server_run_at_hitl_checkpoint_is_normalized() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    client = object.__new__(LangGraphAgentServerClient)
    client._client = SimpleNamespace(runs=_Runs(), threads=_Threads())

    result = await client.get_run(thread_id="thread", run_id="server-run")

    assert result["status"] == "interrupted"
