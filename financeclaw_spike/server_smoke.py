"""Run Agent Server thread/run/stream/HITL checks against a live local endpoint."""

import argparse
import asyncio
from dataclasses import dataclass

from langgraph_sdk import get_client


@dataclass(frozen=True, slots=True)
class ServerSmokeResult:
    thread_id: str
    stream_parts: int
    checkpoint_messages: int
    write_interrupted: bool
    write_resumed: bool


async def probe_agent_server(url: str) -> ServerSmokeResult:
    client = get_client(url=url)
    context = {"request_id": "agent-server-smoke", "allow_write": True, "environment": "test"}
    metadata = {
        "environment": "test",
        "stage": "0",
        "risk_level": "demo",
        "toolset_version": "stage0-v1",
    }

    read_thread = await client.threads.create()
    stream_parts = 0
    async for _part in client.runs.stream(
        read_thread["thread_id"],
        "demo_agent",
        input={"messages": [{"role": "user", "content": "read AAPL"}]},
        context=context,
        metadata=metadata,
        stream_mode=["updates", "values"],
        version="v2",
    ):
        stream_parts += 1
    state = await client.threads.get_state(read_thread["thread_id"])
    checkpoint_messages = len(state["values"]["messages"])

    write_thread = await client.threads.create()
    interrupted = await client.runs.wait(
        write_thread["thread_id"],
        "demo_agent",
        input={"messages": [{"role": "user", "content": "write watchlist"}]},
        context=context,
        metadata=metadata,
    )
    write_interrupted = bool(interrupted.get("__interrupt__"))
    resumed = await client.runs.wait(
        write_thread["thread_id"],
        "demo_agent",
        command={"resume": {"decisions": [{"type": "approve"}]}},
        context=context,
        metadata=metadata,
    )
    write_resumed = len(resumed.get("messages", ())) >= 4
    return ServerSmokeResult(
        thread_id=read_thread["thread_id"],
        stream_parts=stream_parts,
        checkpoint_messages=checkpoint_messages,
        write_interrupted=write_interrupted,
        write_resumed=write_resumed,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    args = parser.parse_args()
    result = asyncio.run(probe_agent_server(args.url))
    if not (
        result.stream_parts
        and result.checkpoint_messages >= 4
        and result.write_interrupted
        and result.write_resumed
    ):
        raise SystemExit(f"Agent Server smoke failed: {result!r}")
    print(result)


if __name__ == "__main__":
    main()
