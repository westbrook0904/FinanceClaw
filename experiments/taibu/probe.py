"""调用真实 MCP、工具包装及 ArtifactService；不调用模型或发送渠道消息。"""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from langchain.tools import ToolRuntime

from financeclaw.agent_server.tools.taibu import taibu_tools
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.releases.taibu import TAIBU_SERVER_VERSION, TAIBU_SOURCE_COMMIT

BAZI_SAMPLE = {
    "gender": "male",
    "calendar_type": "solar",
    "birth_year": 1990,
    "birth_month": 1,
    "birth_day": 15,
    "birth_hour": 9,
    "birth_minute": 0,
    "time_basis": "china_standard",
    "solar_time": "standard",
}


def cases(public: bool) -> list[tuple[str, str, dict]]:
    """公共服务仅黄历；内网增加明确标记为合成资料的八字与农历案例。"""
    result = [("almanac", "taibu_almanac", {"date": "2026-09-12"})]
    if not public:
        result.extend(
            [
                ("bazi_standard", "taibu_bazi", BAZI_SAMPLE),
                (
                    "bazi_true_solar",
                    "taibu_bazi",
                    {**BAZI_SAMPLE, "solar_time": "true_solar", "longitude": 116.4},
                ),
                (
                    "bazi_lunar",
                    "taibu_bazi",
                    {
                        **BAZI_SAMPLE,
                        "calendar_type": "lunar",
                        "birth_day": 1,
                        "is_leap_month": False,
                    },
                ),
                (
                    "bazi_leap_month",
                    "taibu_bazi",
                    {
                        **BAZI_SAMPLE,
                        "calendar_type": "lunar",
                        "birth_year": 2023,
                        "birth_month": 2,
                        "birth_day": 1,
                        "is_leap_month": True,
                    },
                ),
                (
                    "bazi_invalid_leap",
                    "taibu_bazi",
                    {
                        **BAZI_SAMPLE,
                        "calendar_type": "lunar",
                        "birth_year": 2023,
                        "birth_month": 1,
                        "birth_day": 1,
                        "is_leap_month": True,
                    },
                ),
            ]
        )
    return result


async def probe(url: str, *, public: bool) -> dict:
    """临时独立数据库中验证真实工具，保留合成原文证据后删除临时存储。"""
    with TemporaryDirectory(prefix="financeclaw-taibu-probe-") as directory:
        settings = FinanceClawSettings(
            _env_file=None,
            environment="test",
            offline_model=True,
            database_url=f"sqlite+pysqlite:///{directory}/probe.db",
            database_auto_create_schema=True,
            artifact_backend="local",
            artifact_root=f"{directory}/artifacts",
            taibu_enabled=True,
            taibu_mcp_url=url,
            taibu_egress="external" if public else "internal",
            taibu_allowed_hosts=frozenset({urlparse(url).hostname}),
            taibu_allowed_tools=frozenset({"almanac"} if public else {"almanac", "bazi"}),
            debug_full_io=False,
            langsmith_hide_inputs=True,
            langsmith_hide_outputs=True,
            langsmith_trace_sample_rate=0,
        )
        resources = build_resources(settings, enable_persistence=True)
        try:
            tools = {
                item.tool.name: item.tool
                for item in taibu_tools(settings, resources.artifact_service)
            }
            results = []
            for case, name, arguments in cases(public):
                context = ExecutionContext(
                    tenant_id="synthetic-taibu-probe",
                    subject_id="synthetic-subject",
                    turn_id=f"synthetic-{case}",
                    scopes={"taibu:read", "artifacts:read"},
                    data_classification="internal" if public else "confidential",
                    request_clock="2026-09-12T10:00:00+08:00",
                )
                runtime = ToolRuntime(
                    state={},
                    context=context,
                    config={},
                    store=None,
                    stream_writer=lambda _: None,
                    tool_call_id=case,
                )
                start = time.perf_counter()
                response = await tools[name].ainvoke(
                    {
                        "name": name,
                        "id": case,
                        "type": "tool_call",
                        "args": {**arguments, "runtime": runtime},
                    }
                )
                expected_error = case == "bazi_invalid_leap"
                if response.status != ("error" if expected_error else "success"):
                    raise RuntimeError(f"{case}: {response.content}")
                envelope = json.loads(response.content)
                raw = json.loads(
                    resources.artifact_service.read(
                        response.artifact["artifact_id"], context=context
                    )
                )
                if not raw["response"].get("content") or (
                    not expected_error and not raw["response"].get("structuredContent")
                ):
                    raise AssertionError(f"{case}: missing archived MCP channels")
                if expected_error and (
                    raw["response"].get("isError") is not True
                    or "TAIBU_REMOTE_ERROR" not in envelope.get("error", "")
                ):
                    raise AssertionError(f"{case}: invalid lunar date was not a remote error")
                results.append(
                    {
                        "case": case,
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
                        "projection_bytes": len(response.content.encode()),
                        "projection": envelope,
                        "archived_snapshot": raw,
                    }
                )
            return {
                "passed": True,
                "checked_at": datetime.now(UTC).isoformat(),
                "url": url,
                "synthetic_only": True,
                "source_commit": TAIBU_SOURCE_COMMIT,
                "server_version": TAIBU_SERVER_VERSION,
                "dependencies": {
                    name: version(name) for name in ("langchain", "langchain-mcp-adapters", "mcp")
                },
                "scope": "real HTTP, SDK, local tools and temporary artifacts; no model/channel",
                "artifact_storage": "temporary storage removed; synthetic snapshots embedded below",
                "cases": results,
            }
        finally:
            resources.database.close()


def main() -> None:
    """只接受服务地址和证据路径，CLI 不接收任何真实出生资料。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--public", action="store_true", help="external HTTPS; almanac only")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = asyncio.run(probe(args.url, public=args.public))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": True, "cases": len(evidence["cases"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
