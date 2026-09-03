"""提供 server smoke 运维命令的可调用入口。"""

import argparse
import asyncio
from dataclasses import dataclass

from langgraph_sdk import get_client

from financeclaw.kernel import ExecutionContext


@dataclass(frozen=True, slots=True)
class Stage1ServerSmokeResult:
    """定义Stage1服务端Smoke的执行结果。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        finance_agent_stream_parts: 顶层 Agent 冒烟请求收到的流片段数量。
        direct_read_succeeded: 直接读取工具是否在无需审批的情况下成功完成。
        write_interrupted: 写工具首次调用是否按策略进入审批中断。
        edit_reinterrupted: 审批编辑参数后是否重新进入与新参数绑定的中断。
        write_approved: 写工具在批准后是否成功完成。
    """

    finance_agent_stream_parts: int
    direct_read_succeeded: bool
    write_interrupted: bool
    edit_reinterrupted: bool
    write_approved: bool


async def probe_agent_server(url: str) -> Stage1ServerSmokeResult:
    """顺序验证 Agent 流、直接读取、写入审批、编辑重审批和批准恢复。"""
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
    """解析命令行参数，执行 server smoke 操作并输出结果。"""
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
