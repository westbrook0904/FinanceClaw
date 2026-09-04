"""Stage 6 run 级订阅与稳定流事件投影契约测试。"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from financeclaw.application.streaming import completed_stream_event, project_server_part
from financeclaw.infrastructure.clients import LangGraphAgentServerClient


class _FakeRuns:
    """记录 ``join_stream`` 调用并返回真正异步迭代器的 SDK 替身。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.calls: list[dict[str, Any]] = []

    def join_stream(
        self,
        thread_id: str,
        run_id: str,
        *,
        stream_mode: tuple[str, ...],
        headers: dict[str, str] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """记录参数并返回异步生成器。"""
        self.calls.append(
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "stream_mode": stream_mode,
                "headers": headers,
            }
        )

        async def parts() -> AsyncIterator[dict[str, Any]]:
            """产出一个 messages 事件。"""
            yield {
                "event": "messages",
                "data": [{"type": "AIMessageChunk", "content": "hello"}, {}],
            }

        return parts()


class _FakeSDKClient:
    """仅暴露 runs 子客户端的 LangGraph SDK 替身。"""

    def __init__(self) -> None:
        """创建 runs 替身。"""
        self.runs = _FakeRuns()


@pytest.mark.asyncio
async def test_agent_server_client_joins_exact_server_run() -> None:
    """验证真实适配器使用 server_run_id 与三个所需流模式。"""
    client = object.__new__(LangGraphAgentServerClient)
    sdk = _FakeSDKClient()
    client._client = sdk  # type: ignore[attr-defined]
    client._headers = {"Authorization": "Bearer internal"}  # type: ignore[attr-defined]

    parts = [
        part
        async for part in client.stream_run(
            thread_id="thread-1",
            run_id="server-run-1",
        )
    ]

    assert parts[0]["event"] == "messages"
    assert sdk.runs.calls == [
        {
            "thread_id": "thread-1",
            "run_id": "server-run-1",
            "stream_mode": ("messages", "updates", "values"),
            "headers": {"Authorization": "Bearer internal"},
        }
    ]


def test_server_parts_are_projected_to_small_stable_event_set() -> None:
    """验证仅公开助手文本和脱敏进度，不透传用户消息或节点载荷。"""
    delta = project_server_part(
        {
            "event": "messages",
            "data": [{"type": "AIMessageChunk", "content": "增量"}, {"node": "model"}],
        }
    )
    user = project_server_part(
        {"event": "messages", "data": [{"type": "human", "content": "秘密输入"}, {}]}
    )
    progress = project_server_part(
        {"event": "updates", "data": {"tool": {"api_key": "must-not-leak"}}}
    )

    assert delta is not None
    assert delta.event == "assistant.delta"
    assert delta.data == {"delta": "增量"}
    assert user is None
    assert progress is not None
    assert progress.event == "run.progress"
    assert progress.data == {"status": "running"}

    completed = completed_stream_event(
        "run-1",
        {
            "messages": [{"type": "assistant", "content": "最终答复"}],
            "private_state": {"api_key": "must-not-leak"},
        },
    )
    assert completed.event == "assistant.completed"
    assert completed.data == {"run_id": "run-1", "content": "最终答复"}
