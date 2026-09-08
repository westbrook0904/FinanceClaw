"""一次实验独占的 Agent Server 与认证回调接收器，退出时只清理自己的进程。"""

import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from financeclaw.coordination.backends.langgraph_protocol import decode_notification
from financeclaw.kernel.coordination import BackendNotification

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    """分配实验本机端口；启动失败会明确报错而不接管其他服务。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebhookSink:
    """仅用于探针的 HTTP Ingress；记录摘要和字段名，绝不记录令牌或原始值。"""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.events: list[dict[str, Any]] = []
        self.attempts = 0
        self.unauthorized = 0
        self.failures_left = 0
        self.drop = False
        self.on_event: Callable[[BackendNotification], None] | None = None
        self.lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            """认证先于解析，成功回执晚于持久化 hook。"""

            def do_POST(self) -> None:
                """限制路径和 body；故障开关用于验证重试、丢失和拒绝行为。"""
                if self.path != "/internal/coordinator/webhooks/langgraph/probe":
                    self.send_error(404)
                    return
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + sink.token
                ):
                    with sink.lock:
                        sink.unauthorized += 1
                    self.send_error(401)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                try:
                    notification = decode_notification(body, backend_instance_id="probe")
                except (KeyError, TypeError, ValueError):
                    self.send_error(400)
                    return
                with sink.lock:
                    sink.attempts += 1
                    fail = sink.failures_left > 0
                    sink.failures_left = max(0, sink.failures_left - 1)
                if fail:
                    self.send_error(503)
                    return
                if not sink.drop:
                    try:
                        if sink.on_event:
                            sink.on_event(notification)
                    except Exception:
                        self.send_error(503)
                        return
                    with sink.lock:
                        sink.events.append(
                            {
                                "notification": notification.model_dump(mode="json"),
                                "fields": sorted(json.loads(body)),
                            }
                        )
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                """禁用默认 HTTP 日志；实验报告只含结构化计数和断言。"""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = (
            f"http://127.0.0.1:{self.server.server_port}"
            "/internal/coordinator/webhooks/langgraph/probe"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        """启动独立的回调监听线程。"""
        self.thread.start()

    def close(self) -> None:
        """关闭本实验的监听套接字与线程。"""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextmanager
def agent_server(
    directory: Path, sink: WebhookSink, *, filter_fields: bool = False
) -> Iterator[str]:
    """在临时目录启动真实 langgraph-api；不加载项目 .env，不调用线上模型。"""
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "langgraph.json"
    config.write_text(
        json.dumps(
            {
                "dependencies": [str(ROOT)],
                "graphs": {
                    name: f"{ROOT / 'experiments/stage8/graphs.py'}:{name}"
                    for name in ("parent", "child", "success", "failure", "slow")
                },
                "env": {},
                "webhooks": {
                    "headers": {"Authorization": "Bearer ${{ env.LG_WEBHOOK_PROBE_TOKEN }}"},
                    # loopback exception is confined to this local synthetic experiment.
                    "url": {
                        "allowed_domains": ["127.0.0.1"],
                        "allowed_ports": [sink.server.server_port],
                        "disable_loopback": False,
                        "require_https": False,
                    },
                },
            }
        )
    )
    if filter_fields:
        value = json.loads(config.read_text())
        value["webhooks"]["allowed_fields"] = ["run_id", "thread_id", "status", "webhook_sent_at"]
        config.write_text(json.dumps(value))
    environment = {
        key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "SYSTEMROOT"}
    }
    environment.update(
        PYTHONPATH=str(ROOT),
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        LANGGRAPH_AUTH_TYPE="noop",
        LANGGRAPH_API_DO_NOT_TRACK="true",
        LG_WEBHOOK_PROBE_TOKEN=sink.token,
        NO_PROXY="127.0.0.1,localhost",
    )
    port = free_port()
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(
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
                str(port),
                "--no-browser",
                "--no-reload",
                "--allow-blocking",
                "--server-log-level",
                "WARNING",
            ],
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"probe Agent Server exited; inspect {directory / 'server.log'}"
                    )
                try:
                    if httpx.get(url + "/ok", timeout=1, trust_env=False).is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("probe Agent Server did not start")
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
