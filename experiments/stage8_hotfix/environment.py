"""HF-0 独占本机 Agent Server；使用空 env 配置和进程环境白名单。"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from experiments.stage8_hotfix.graphs import ROOT_GRAPH_ID

ROOT = Path(__file__).resolve().parents[2]


def isolated_environment(event_log: Path) -> dict[str, str]:
    """不加载任何 .env、模型 key、服务 license 或 tracing key。"""
    env = {
        key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "SYSTEMROOT"}
    }
    env.update(
        PYTHONPATH=str(ROOT),
        HF0_EVENT_LOG=str(event_log),
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        LANGGRAPH_AUTH_TYPE="noop",
        LANGGRAPH_API_DO_NOT_TRACK="true",
        NO_PROXY="127.0.0.1,localhost",
    )
    return env


class NativeServer:
    """只启动／终止本探针创建的进程组，不接管现有服务。"""

    def __init__(self, directory: Path):
        self.directory = directory
        self.process = None
        self.log = None
        self.event_log = directory / "events.jsonl"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        """无 reload、无浏览器，所有图和模型只消费合成数据。"""
        self.directory.mkdir(parents=True, exist_ok=False)
        config = self.directory / "langgraph.json"
        config.write_text(
            json.dumps(
                {
                    "dependencies": [str(ROOT)],
                    "graphs": {
                        ROOT_GRAPH_ID: f"{ROOT}/experiments/stage8_hotfix/server.py:orchestrator"
                    },
                    "env": {},
                }
            )
        )
        self.event_log.touch()
        self.log = (self.directory / "agent-server.log").open("w")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "langgraph_cli",
                    "dev",
                    "--config",
                    str(config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-browser",
                    "--no-reload",
                    "--allow-blocking",
                    "--server-log-level",
                    "WARNING",
                ],
                cwd=self.directory,
                env=isolated_environment(self.event_log),
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"HF-0 Agent Server exited; inspect {self.directory}")
                try:
                    if httpx.get(self.url + "/ok", timeout=1, trust_env=False).is_success:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("HF-0 Agent Server startup timed out")
        except BaseException:
            self.close()
            raise

    def events(self, root_id: str) -> list[dict]:
        """运行暂停／结束后读取实际节点计数。"""
        return [
            row
            for line in self.event_log.read_text().splitlines()
            if (row := json.loads(line))["root_id"] == root_id
        ]

    def close(self) -> None:
        """有界退出本实例进程；异常路径同样回收监听端口。"""
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def __exit__(self, *_):
        """完成或失败都结束隔离服务。"""
        self.close()
