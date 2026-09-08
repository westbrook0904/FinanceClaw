"""隔离的正式 BFF、Ingress、Worker 与真实 Agent Server 进程。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from experiments.stage8.environment import free_port

ROOT = Path(__file__).resolve().parents[2]


class Processes:
    """只管理本次验证创建的进程；日志不进入永久证据。"""

    def __init__(self, directory, database):
        """所有角色共享一个独占业务库；不加载仓库 .env 或线上模型凭据。"""
        import secrets

        self.directory, self.processes, self.logs = directory, {}, []
        self.agent_port, self.ingress_port, self.bff_port = free_port(), free_port(), free_port()
        self.token = secrets.token_urlsafe(32)
        self.env = {key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR"}}
        self.env.update(
            {
                "PYTHONPATH": str(ROOT),
                "NO_PROXY": "127.0.0.1,localhost",
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
                "LANGGRAPH_AUTH_TYPE": "noop",
                "LANGGRAPH_API_DO_NOT_TRACK": "true",
                "FINANCECLAW_ENVIRONMENT": "test",
                "FINANCECLAW_OFFLINE_MODEL": "true",
                "FINANCECLAW_DATABASE_URL": database.replace(
                    "postgresql://", "postgresql+psycopg://"
                ),
                "FINANCECLAW_DATABASE_AUTO_CREATE_SCHEMA": "false",
                "FINANCECLAW_AGENT_SERVER_URL": self.agent_url,
                "FINANCECLAW_ARTIFACT_ROOT": str(directory / "artifacts"),
                "FINANCECLAW_COORDINATOR_ENABLED": "true",
                "FINANCECLAW_COORDINATOR_CALLBACK_URL": self.callback_url,
                "FINANCECLAW_COORDINATOR_WEBHOOK_TOKEN": self.token,
                "LG_WEBHOOK_COORDINATOR_TOKEN": self.token,
                "FINANCECLAW_COORDINATOR_POLL_SECONDS": "0.1",
                "FINANCECLAW_COORDINATOR_RECONCILE_SECONDS": "0.3",
                "FINANCECLAW_COORDINATOR_LEASE_SECONDS": "6",
                "FINANCECLAW_BFF_AUTH_TOKEN": self.token,
                "FINANCECLAW_BFF_SCOPES": json.dumps(
                    [
                        "market:read",
                        "tools:read",
                        "tools:approve",
                        "artifacts:read",
                        "portfolio:review",
                        "workflows:approve",
                        "watchlist:write",
                    ]
                ),
                "FINANCECLAW_FEISHU_ENABLED": "false",
            }
        )

    @property
    def agent_url(self):
        """真实 Agent Server 的本机隔离监听地址。"""
        return f"http://127.0.0.1:{self.agent_port}"

    @property
    def bff_url(self):
        """BFF 只在需要受理用户命令时启动。"""
        return f"http://127.0.0.1:{self.bff_port}"

    @property
    def callback_url(self):
        """每个原生 start/resume 都携带正式 Ingress 地址。"""
        return f"http://127.0.0.1:{self.ingress_port}/internal/webhooks/langgraph-primary"

    def start(self, name, args):
        """创建独立 OS 进程，输出仅保存在临时目录。"""
        if name in self.processes:
            raise RuntimeError("probe process is already running")
        log = (self.directory / (name + ".log")).open("a")
        self.logs.append(log)
        self.processes[name] = subprocess.Popen(
            [sys.executable, *args],
            cwd=self.directory,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def wait_http(self, name, url):
        """等待服务启动失败时返回日志位置，不输出潜在敏感响应。"""
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.processes[name].poll() is not None:
                raise RuntimeError(f"{name} exited; inspect {self.directory / (name + '.log')}")
            try:
                if httpx.get(url, timeout=1, trust_env=False).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"{name} did not start")

    def graph_config(self):
        """本次隔离图与受认证 Webhook 配置。"""
        return {
            "dependencies": [str(ROOT)],
            "graphs": {
                "finance_agent_v1_4_0": f"{ROOT}/experiments/stage8a/graphs.py:finance_agent",
                "market_research_agent_v1_2_0": (
                    f"{ROOT}/experiments/stage8a/graphs.py:market_research_agent"
                ),
                "portfolio_review_v1": f"{ROOT}/experiments/stage8a/graphs.py:portfolio_review_v1",
            },
            "env": {},
            "webhooks": {
                "headers": {"Authorization": "Bearer ${{ env.LG_WEBHOOK_COORDINATOR_TOKEN }}"},
                "url": {
                    "allowed_domains": ["127.0.0.1"],
                    "allowed_ports": [self.ingress_port],
                    "disable_loopback": False,
                    "require_https": False,
                },
            },
        }

    def agent(self):
        """真实 native API／checkpoint／interrupt／webhook，受治理发布图与合成模型。"""
        path = self.directory / "langgraph.json"
        path.write_text(json.dumps(self.graph_config()))
        self.start(
            "agent",
            [
                "-m",
                "langgraph_cli",
                "dev",
                "--config",
                str(path),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.agent_port),
                "--no-browser",
                "--no-reload",
                "--allow-blocking",
                "--server-log-level",
                "WARNING",
            ],
        )
        self.wait_http("agent", self.agent_url + "/ok")

    def ingress(self):
        """启动正式 Ingress 工厂，只有 HTTP 接收与持久化职责。"""
        self.start(
            "ingress",
            [
                "-m",
                "uvicorn",
                "financeclaw.coordination.ingress.app:create_default_ingress",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.ingress_port),
                "--no-access-log",
            ],
        )
        self.wait_http("ingress", f"http://127.0.0.1:{self.ingress_port}/health")

    def bff(self):
        """启动正式 BFF，在受理后结束进程以排除查询驱动。"""
        self.start(
            "bff",
            [
                "-m",
                "uvicorn",
                "financeclaw.bff.bootstrap:create_default_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.bff_port),
                "--no-access-log",
            ],
        )
        self.wait_http("bff", self.bff_url + "/health")

    def workers(self, *, lose_receipts=False):
        """至少两个独立 Worker，故障模式仅丢掉真实 HTTP 成功回执。"""
        module = (
            "experiments.stage8a.receipt_worker"
            if lose_receipts
            else "financeclaw.coordination.worker"
        )
        for i in range(2):
            self.start(f"worker-{i}", ["-m", module])

    def stop(self, name, *, kill=False):
        """只停止本次创建的进程，可注入 SIGKILL 验证租约接管。"""
        process = self.processes.pop(name, None)
        if process:
            process.kill() if kill else process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        """逆序释放所有本次监听与日志句柄。"""
        for name in reversed(list(self.processes)):
            self.stop(name)
        for log in self.logs:
            log.close()
