"""运行 HF-0 原生图／真实 HTTP 探针并保存有源码指纹的合成证据。"""

import argparse
import asyncio
import json
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from experiments.stage8_hotfix.environment import ROOT
from experiments.stage8_hotfix.local_probe import run_local
from experiments.stage8_hotfix.native_probe import run_native


def provenance() -> dict:
    """记录实际版本与探针源码，不读取环境变量值或 Git 工作区内容。"""
    packages = (
        "langchain",
        "langchain-core",
        "langgraph",
        "langgraph-sdk",
        "langgraph-api",
        "langgraph-cli",
        "langgraph-runtime-inmem",
        "langgraph-checkpoint",
        "httpx",
        "pydantic",
    )
    sources = sorted((ROOT / "experiments/stage8_hotfix").glob("*.py"))
    sources += sorted((ROOT / "tests/stage8_hotfix").glob("*.py"))
    sources.append(ROOT / "experiments/stage8_hotfix/requirements.txt")
    return {
        "python": platform.python_version(),
        "packages": {p: version(p) for p in packages},
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sources
        },
        "release_plan_status": "reserved_for_hf1_not_registered",
    }


async def run(args) -> dict:
    """先本地图再 HTTP；任一断言失败就不给出阶段通过结论。"""
    report = {
        "schema_version": 1,
        "stage": "stage-8-hotfix-hf0",
        "recorded_at": datetime.now(UTC).isoformat(),
        "provenance": provenance(),
        "passed": False,
        "hf0_complete": False,
        "scope": {
            "synthetic_model": True,
            "real_credentials_used": False,
            "production_code_switched": False,
            "persistent_runtime_restart_verified": False,
            "bff_journal_delivery_verified": False,
        },
    }
    try:
        report["local"] = await run_local()
        print("HF-0 local: 9/9 passed", flush=True)
        if not args.local_only:
            directory = args.directory or Path(tempfile.mkdtemp(prefix="financeclaw-hf0-"))
            directory = directory.resolve()
            directory.mkdir(parents=True, exist_ok=True)
            print(f"HF-0 native logs: {directory / 'native'}", flush=True)
            report["native"] = await run_native(directory / "native")
            print("HF-0 native HTTP: 9/9 passed", flush=True)
            report["hf0_complete"] = True
        report["passed"] = True
        return report
    except BaseException as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    """显式指定证据落点；临时服务目录只能用于一次运行。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--local-only", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
