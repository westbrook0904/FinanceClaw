"""只在验证进程中丢弃成功 HTTP 回执，正式 Worker 与持久化代码保持不变。"""

import asyncio
import signal

from financeclaw.coordination.bootstrap import build_coordinator
from financeclaw.coordination.worker.__main__ import run_worker


async def main():
    """每个唯一提交都真实创建，然后模拟接收回执前断连。"""
    services, coordinator = build_coordinator()
    backend = coordinator.backend
    submit, deliver = backend.submit_task, backend.deliver_response

    async def lost_submit(command):
        """远端提交成功后中断；Worker 必须查原 metadata，而不能重发。"""
        await submit(command)
        raise ConnectionError("synthetic receipt loss")

    async def lost_delivery(command):
        """原位恢复成功后也丢回执，交付与执行必须独立核对。"""
        await deliver(command)
        raise ConnectionError("synthetic receipt loss")

    backend.submit_task, backend.deliver_response = lost_submit, lost_delivery
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await run_worker(coordinator, stop)
    finally:
        services.resources.database.close()


if __name__ == "__main__":
    asyncio.run(main())
