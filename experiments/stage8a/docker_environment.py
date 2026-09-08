"""使用原样自托管镜像验证 PostgreSQL／Redis Agent runtime；不修改许可或运行时实现。"""

import json
import subprocess
from uuid import uuid4

from experiments.stage8.run import isolated_database
from experiments.stage8a.environment import ROOT, Processes


class DockerProcesses(Processes):
    """只替换 Agent Server 的启动形式，其余角色继续使用正式进程入口。"""

    def __init__(self, directory, database, *, image, postgres_url, redis_url, license_env=None):
        """Agent runtime 和业务事实用不同数据库，镜像和 Redis 由调用方明确提供。"""
        self.image, self.redis_url = image, redis_url
        self.container_name = "financeclaw-stage8a-agent-" + uuid4().hex[:10]
        self.runtime_database = isolated_database(postgres_url)
        self.license = {}
        if license_env:
            from dotenv import dotenv_values

            values = dotenv_values(license_env)
            self.license = {
                key: values[key]
                for key in ("LANGSMITH_API_KEY", "LANGGRAPH_CLOUD_LICENSE_KEY")
                if values.get(key)
            }
        super().__init__(directory, database)

    @property
    def callback_url(self):
        """Docker Desktop 通过 host.docker.internal 访问本次 Ingress。"""
        return (
            f"http://host.docker.internal:{self.ingress_port}/internal/webhooks/langgraph-primary"
        )

    def agent(self):
        """启动未修改的目标镜像，使用合成模型、隔离 DB 与默认许可检查。"""
        config = self.graph_config()
        config["webhooks"]["url"]["allowed_domains"] = ["host.docker.internal"]
        environment = {
            key: value for key, value in self.env.items() if key not in {"PATH", "TMPDIR"}
        }
        environment.update(
            {
                "LANGSERVE_GRAPHS": json.dumps(config["graphs"]),
                "LANGGRAPH_WEBHOOKS": json.dumps(config["webhooks"]),
                "DATABASE_URI": self.runtime_database.replace("127.0.0.1", "host.docker.internal"),
                "REDIS_URI": self.redis_url,
                "PORT": "8000",
                "LANGSMITH_API_KEY": "",
                "LANGCHAIN_API_KEY": "",
                "FINANCECLAW_DATABASE_URL": self.env["FINANCECLAW_DATABASE_URL"].replace(
                    "127.0.0.1", "host.docker.internal"
                ),
            }
        )
        environment.update(self.license)
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            self.container_name,
            "-p",
            f"127.0.0.1:{self.agent_port}:8000",
            "-v",
            f"{ROOT}:{ROOT}:ro",
            "-v",
            f"{self.directory}:{self.directory}",
            "--workdir",
            str(self.directory),
        ]
        for key, value in environment.items():
            args.extend(["-e", f"{key}={value}"])
        args.append(self.image)
        log = (self.directory / "agent.log").open("a")
        self.logs.append(log)
        self.processes["agent"] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        self.wait_http("agent", self.agent_url + "/ok")

    def stop(self, name, *, kill=False):
        """关闭本次镜像实例，保证停止 CLI 时不遗留后台容器。"""
        if name == "agent" and name in self.processes:
            subprocess.run(
                ["docker", "kill" if kill else "stop", self.container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        super().stop(name, kill=kill)
