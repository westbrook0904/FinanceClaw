"""Current channel and resource boundary regressions."""

import asyncio
from threading import BoundedSemaphore
from types import SimpleNamespace

import pytest

from financeclaw.agent_server.middleware.execution_middleware import ExecutionBudgetMiddleware
from tests.stage1.test_agent import components_with_tools, context


@pytest.mark.asyncio
async def test_worker_resource_gate_limits_and_releases_on_error():
    """原生并发工具仍受资源门控，异常与取消不泄漏 semaphore。"""
    components, _ = components_with_tools()
    middleware = ExecutionBudgetMiddleware(None, components.tool_catalog, BoundedSemaphore(2))
    request = SimpleNamespace(runtime=SimpleNamespace(context=context("tools:read")))
    active = maximum = 0

    async def handler(_):
        """用短异步 I/O 观察并发峰值，并注入一个可恢复错误。"""
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            raise ValueError("tool failure")
        finally:
            active -= 1

    results = await asyncio.gather(
        *(middleware.awrap_tool_call(request, handler) for _ in range(8)), return_exceptions=True
    )
    assert maximum == 2 and all(isinstance(item, ValueError) for item in results)
    assert middleware.gate.acquire(blocking=False) and middleware.gate.acquire(blocking=False)
    middleware.gate.release()
    middleware.gate.release()
