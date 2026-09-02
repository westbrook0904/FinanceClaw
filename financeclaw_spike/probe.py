"""Command-line entry point for credential/service-gated Stage-0 probes."""

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Any

from financeclaw_spike.infrastructure import probe_services
from financeclaw_spike.mcp import load_demo_mcp_tool
from financeclaw_spike.provider import probe_provider
from financeclaw_spike.settings import SpikeSettings


async def _run(probe: str) -> dict[str, Any]:
    settings = SpikeSettings()
    if probe == "provider":
        result = await probe_provider(settings)
        payload = asdict(result)
        payload["structured_output"] = result.structured_output.model_dump(mode="json")
        return payload
    if probe == "services":
        return asdict(await probe_services(settings))
    tool, governance = await load_demo_mcp_tool(timeout_seconds=settings.mcp_timeout_seconds)
    result = await tool.ainvoke({"symbol": "AAPL"})
    return {"tool": tool.name, "governance": asdict(governance), "result": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("probe", choices=("mcp", "provider", "services"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args.probe)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
