"""本地离线 Agent Server 冒烟命令（Stage-1）。

通过 LangGraph SDK 直连本地 Agent Server（默认 127.0.0.1:2024），覆盖四条
链路：finance_agent 流式对话、direct_tool 只读调用，以及经 HITL 审批的写
调用（编辑后再次中断、批准后完成）。运行方式：
``python -m financeclaw.operations.server_smoke``。
"""

import argparse
import asyncio
from dataclasses import dataclass

from langgraph_sdk import get_client

from financeclaw.kernel import ExecutionContext


@dataclass(frozen=True, slots=True)
class Stage1ServerSmokeResult:
    """Stage-1 Agent Server 冒烟的结果快照。

    使用场景：``probe_agent_server`` 返回后由 ``main`` 做全量真值校验，
    任一字段为假值即判定冒烟失败并以非零码退出。

    Attributes:
        finance_agent_stream_parts: finance_agent 流式返回的事件片段数。
        direct_read_succeeded: direct_tool 只读调用（market_snapshot）是否成功。
        write_interrupted: 写调用（watchlist_add）是否如预期触发审批中断。
        edit_reinterrupted: 编辑决定恢复后是否再次进入审批中断。
        write_approved: 批准决定恢复后调用是否成功完成。

    """

    finance_agent_stream_parts: int
    direct_read_succeeded: bool
    write_interrupted: bool
    edit_reinterrupted: bool
    write_approved: bool


async def probe_agent_server(url: str) -> Stage1ServerSmokeResult:
    """对本地 Agent Server 依次执行流式对话、只读与写审批链路探针。

    Args:
        url: Agent Server 基础地址。

    Returns:
        四条链路的冒烟结果。

    """
    # 1. 构造 SDK 客户端与固定租户/主体的执行上下文与元数据。
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
    # 2. 流式链路：在新 thread 上流式调用 finance_agent 并统计事件片段数。
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
    # 3. 只读链路：direct_tool 调用 market_snapshot，校验返回成功状态。
    read_thread = await client.threads.create()
    direct_read = await client.runs.wait(
        read_thread["thread_id"],
        "direct_tool",
        input={"tool_id": "market_snapshot", "version": None, "arguments": {"symbol": "AAPL"}},
        context=context,
        metadata=metadata,
    )
    direct_read_succeeded = direct_read.get("response", {}).get("status") == "success"
    # 4. 写链路：direct_tool 调用 watchlist_add，应触发审批中断。
    write_thread = await client.threads.create()
    interrupted = await client.runs.wait(
        write_thread["thread_id"],
        "direct_tool",
        input={"tool_id": "watchlist_add", "version": None, "arguments": {"symbol": "AAPL"}},
        context=context,
        metadata=metadata,
    )
    write_interrupted = bool(interrupted.get("__interrupt__"))
    # 5. 恢复链路（编辑）：改写参数恢复后应再次进入审批中断。
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
    # 6. 恢复链路（批准）：批准后调用应成功完成。
    approved = await client.runs.wait(
        write_thread["thread_id"],
        "direct_tool",
        command={"resume": {"decisions": [{"type": "approve"}]}},
        context=context,
        metadata=metadata,
    )
    write_approved = approved.get("response", {}).get("status") == "success"
    # 7. 汇总四条链路的冒烟结果。
    return Stage1ServerSmokeResult(
        finance_agent_stream_parts=stream_parts,
        direct_read_succeeded=direct_read_succeeded,
        write_interrupted=write_interrupted,
        edit_reinterrupted=edit_reinterrupted,
        write_approved=write_approved,
    )


def main() -> None:
    """执行本地 Agent Server 冒烟，链路不完整时以非零码退出。"""
    # 1. 解析命令行参数（Server 地址）。
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    args = parser.parse_args()
    # 2. 执行冒烟探针。
    result = asyncio.run(probe_agent_server(args.url))
    # 3. 校验全部链路均为真值，否则判冒烟失败。
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
    # 4. 打印结果。
    print(result)


if __name__ == "__main__":
    main()
