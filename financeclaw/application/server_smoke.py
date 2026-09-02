"""Live Agent Server Stage-1 run/stream/direct approval smoke probe."""

import argparse
import asyncio
from dataclasses import dataclass

from langgraph_sdk import get_client

from financeclaw.contracts import ExecutionContext


@dataclass(frozen=True, slots=True)
class Stage1ServerSmokeResult:
    finance_agent_stream_parts: int
    direct_read_succeeded: bool
    write_interrupted: bool
    edit_reinterrupted: bool
    write_approved: bool


async def probe_agent_server(url: str) -> Stage1ServerSmokeResult:
    client = get_client(url=url)
    context = ExecutionContext(
        tenant_id="stage1-smoke",
        subject_id="stage1-smoke",
        scopes={"market:read", "tools:read", "watchlist:write"},
        turn_id="turn-stage1-smoke",
        run_id="run-stage1-smoke",
        data_classification="internal",
    ).model_dump(mode="json")
    metadata = {"environment": "test", "stage": "1", "risk_level": "smoke"}

    agent_thread = await client.threads.create()
    stream_parts = 0
    async for _part in client.runs.stream(
        agent_thread["thread_id"],
        "finance_agent",
        input={"messages": [{"role": "user", "content": "read AAPL"}]},
        context=context,
        metadata=metadata,
        stream_mode=["updates", "values"],
        version="v2",
    ):
        stream_parts += 1

    read_thread = await client.threads.create()
    direct_read = await client.runs.wait(
        read_thread["thread_id"],
        "direct_tool",
        input={"tool_id": "market_snapshot", "version": None, "arguments": {"symbol": "AAPL"}},
        context=context,
        metadata=metadata,
    )
    direct_read_succeeded = direct_read.get("response", {}).get("status") == "success"

    write_thread = await client.threads.create()
    interrupted = await client.runs.wait(
        write_thread["thread_id"],
        "direct_tool",
        input={"tool_id": "watchlist_add", "version": None, "arguments": {"symbol": "AAPL"}},
        context=context,
        metadata=metadata,
    )
    write_interrupted = bool(interrupted.get("__interrupt__"))
    edited = await client.runs.wait(
        write_thread["thread_id"],
        "direct_tool",
        command={
            "resume": {
                "decisions": [
                    {
                        "type": "edit",
                        "edited_action": {
                            "name": "watchlist_add",
                            "args": {"symbol": "MSFT", "note": "edited"},
                        },
                    }
                ]
            }
        },
        context=context,
        metadata=metadata,
    )
    edit_reinterrupted = bool(edited.get("__interrupt__"))
    approved = await client.runs.wait(
        write_thread["thread_id"],
        "direct_tool",
        command={"resume": {"decisions": [{"type": "approve"}]}},
        context=context,
        metadata=metadata,
    )
    write_approved = approved.get("response", {}).get("status") == "success"
    return Stage1ServerSmokeResult(
        finance_agent_stream_parts=stream_parts,
        direct_read_succeeded=direct_read_succeeded,
        write_interrupted=write_interrupted,
        edit_reinterrupted=edit_reinterrupted,
        write_approved=write_approved,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    args = parser.parse_args()
    result = asyncio.run(probe_agent_server(args.url))
    if not all(
        (
            result.finance_agent_stream_parts,
            result.direct_read_succeeded,
            result.write_interrupted,
            result.edit_reinterrupted,
            result.write_approved,
        )
    ):
        raise SystemExit(f"Stage-1 Agent Server smoke failed: {result!r}")
    print(result)


if __name__ == "__main__":
    main()
