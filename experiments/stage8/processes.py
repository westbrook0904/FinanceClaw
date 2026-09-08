"""使用独立 OS Worker 进程，验证进程重启与共享业务事实。"""

import asyncio
import multiprocessing
from pathlib import Path

from experiments.stage8.backend import ProbeBackend
from experiments.stage8.driver import ProbeDriver
from experiments.stage8.store import ProbeStore


def worker_main(
    database: str,
    server: str,
    callback: str,
    directory: str,
    stop,
    ready,
) -> None:
    """子进程重新装配数据库和 Adapter，不继承父进程连接或内存状态。"""

    async def run() -> None:
        """装配 PostgreSQL 责任领取与业务推进。"""
        store = ProbeStore(database)
        driver = ProbeDriver(store, ProbeBackend(server, callback, Path(directory)))
        try:
            from experiments.stage8.postgres_worker import run_worker

            shutdown = asyncio.Event()
            task = asyncio.create_task(run_worker(driver, shutdown))
            ready.set()
            try:
                while not stop.is_set():
                    if task.done():
                        await task
                    await asyncio.sleep(0.03)
            finally:
                shutdown.set()
                await task
        finally:
            store.db.close()

    asyncio.run(run())


class WorkerProcesses:
    """固定启动两个实验 Worker，支持杀死后从持久化事实重新启动。"""

    def __init__(self, *arguments) -> None:
        self.arguments = arguments
        self.context = multiprocessing.get_context("spawn")
        self.processes = []
        self.stop = self.context.Event()

    async def start(self) -> None:
        """子进程就绪后才受理测试任务。"""
        self.stop.clear()
        for _ in range(2):
            ready = self.context.Event()
            process = self.context.Process(
                target=worker_main, args=(*self.arguments, self.stop, ready)
            )
            process.start()
            self.processes.append(process)
            async with asyncio.timeout(25):
                while not ready.is_set():
                    if not process.is_alive():
                        raise RuntimeError("probe worker exited during startup")
                    await asyncio.sleep(0.05)

    def kill(self) -> None:
        """模拟进程崩溃，只终止当前实验创建的 PID。"""
        for process in self.processes:
            process.kill()
            process.join(timeout=5)
        self.processes.clear()

    async def close(self) -> None:
        """优雅退出，超时才杀死本实验 Worker。"""
        self.stop.set()
        deadline = asyncio.get_running_loop().time() + 15
        while any(process.is_alive() for process in self.processes):
            if asyncio.get_running_loop().time() > deadline:
                self.kill()
                return
            await asyncio.sleep(0.05)
        for process in self.processes:
            if process.exitcode:
                raise RuntimeError(f"probe worker exited with {process.exitcode}")
            process.join()
        self.processes.clear()
