"""准备固定 MCP 定义，再构建和启动仓库的 Docker Compose 部署。"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from financeclaw.shared.mcp.configuration import MCPConfiguration

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_ROOT = PurePosixPath("/app/financeclaw")


class DeploymentError(RuntimeError):
    """可直接显示的部署配置错误，不包含展开后的环境值。"""


def local_config(value: str, root: Path) -> Path:
    """将容器内 TOML 路径映射到 Dockerfile 已打包的 config 目录。"""
    path = PurePosixPath(value)
    if path.is_absolute():
        if not path.is_relative_to(CONTAINER_ROOT):
            raise DeploymentError("MCP 配置须位于镜像 /app/financeclaw/config 目录。")
        path = path.relative_to(CONTAINER_ROOT)
    result = (root / str(path)).resolve()
    if result.parent != root / "config" or result.suffix != ".toml":
        raise DeploymentError("部署入口要求 MCP 配置使用 config/*.toml，以便打入统一镜像。")
    return result


def preparation_inputs(services: dict, root: Path) -> tuple[Path, dict[str, str]]:
    """使用 Compose 实际 Worker 环境导入，避免本机变量掩盖容器缺失的凭据。"""
    api = services["api"].get("environment", {})
    worker = services["worker"].get("environment", {})
    name = "FINANCECLAW_MCP_CONFIG_PATH"
    path = local_config(worker.get(name) or "config/mcp.toml", root)
    if local_config(api.get(name) or "config/mcp.toml", root) != path:
        raise DeploymentError("API 与 Worker 的 MCP 配置路径不同，请先统一 Compose 配置。")
    configuration = MCPConfiguration.from_file(str(path))
    references = set()
    for server in configuration.servers.values():
        if not server.enabled:
            continue
        manifest = (path.parent / server.contracts).resolve()
        if not manifest.is_relative_to(root / "config" / "mcp"):
            raise DeploymentError("已启用 MCP 的 contracts 必须位于 config/mcp/，才能打入镜像。")
        if server.url_env and api.get(server.url_env) != worker.get(server.url_env):
            raise DeploymentError("API 与 Worker 的 MCP 端点变量不同，请先统一 Compose 配置。")
        references.update(server.auth.headers_env.values())
        references.update(ref for ref in (server.url_env, server.auth.token_env) if ref)
    environment = {key: value for key, value in os.environ.items() if key not in references}
    environment.update({key: str(value) for key, value in worker.items() if value is not None})
    return path, environment


def deploy(args, *, root=ROOT) -> None:
    """准备失败不构建，构建失败不启动；沿用原 Compose 迁移与服务依赖。"""
    root = root.resolve()
    env_file = str((root / args.env_file).resolve())
    environment = {**os.environ, "FINANCECLAW_ENV_FILE": env_file}
    compose = ["docker", "compose", "--env-file", env_file]
    for filename in args.files or ["compose.yml"]:
        compose.extend(["-f", str((root / filename).resolve())])
    print("读取 Compose 部署配置……", flush=True)
    rendered = subprocess.run(
        [*compose, "config", "--format", "json"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if rendered.returncode:
        raise DeploymentError(
            "Compose 配置解析失败；请检查环境文件，或用相同参数运行 docker compose config --quiet。"
        )
    services = json.loads(rendered.stdout)["services"]
    path, preparation_environment = preparation_inputs(services, root)
    prepare = [
        sys.executable,
        str(root / "scripts" / "mcp_catalog.py"),
        "prepare",
        "--config",
        str(path),
        "--env-file",
        "",
    ]
    if args.refresh_mcp:
        prepare.append("--refresh")
    subprocess.run(prepare, cwd=root, env=preparation_environment, check=True)
    if args.prepare_only:
        print("MCP 部署准备完成；尚未构建或启动容器。", flush=True)
        return
    for command, message in (
        (["build"], "构建部署镜像……"),
        (["up", "-d", "--no-build"], "启动服务……"),
        (["ps", "-a"], "查看容器状态（就绪情况以健康检查为准）……"),
    ):
        print(message, flush=True)
        step_environment = environment.copy()
        if command[0] == "build" and args.build_proxy:
            # 宿主机 Docker 客户端仍使用宿主机代理，容器代理仅通过构建参数传递。
            step_environment["FINANCECLAW_BUILD_PROXY"] = args.build_proxy
            proxy_args = {
                name: "${FINANCECLAW_BUILD_PROXY}"
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
            }
            overlay = {
                "services": {
                    name: {"build": {"args": proxy_args}}
                    for name, service in services.items()
                    if service.get("build")
                }
            }
            with TemporaryDirectory(prefix="financeclaw-build-") as directory:
                filename = Path(directory) / "compose.proxy.json"
                filename.write_text(json.dumps(overlay), encoding="utf-8")
                subprocess.run(
                    [*compose, "-f", str(filename), *command],
                    cwd=root,
                    env=step_environment,
                    check=True,
                )
            continue
        subprocess.run([*compose, *command], cwd=root, env=step_environment, check=True)


def main() -> int:
    """提供一条命令部署、仅准备及显式刷新工具定义三个操作方式。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=os.environ.get("FINANCECLAW_ENV_FILE") or ".env")
    parser.add_argument(
        "-f", "--file", dest="files", action="append", help="可重复指定 Compose 文件"
    )
    parser.add_argument("--refresh-mcp", action="store_true", help="刷新所有已启用 MCP 的固定定义")
    parser.add_argument("--prepare-only", action="store_true", help="只准备 MCP，不构建或启动容器")
    parser.add_argument(
        "--build-proxy",
        default=os.environ.get("FINANCECLAW_BUILD_PROXY"),
        help="仅用于构建的 HTTP 代理，也可设置 FINANCECLAW_BUILD_PROXY",
    )
    args = parser.parse_args()
    try:
        deploy(args)
    except DeploymentError as error:
        print(f"部署失败：{error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print("部署步骤失败，已停止后续步骤。", file=sys.stderr)
        return error.returncode if error.returncode > 0 else 1
    except Exception as error:
        print(f"部署失败：{type(error).__name__}，请检查部署配置。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
