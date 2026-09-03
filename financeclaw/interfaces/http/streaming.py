"""把内部流事件编码为 Server-Sent Events 数据帧。"""

import json
from collections.abc import AsyncIterator

from financeclaw.kernel import StreamEvent


async def project_sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """把内部流事件编码为符合 SSE 语法的 `event` 与 `data` 数据帧。"""
    async for event in events:
        payload = json.dumps(event.data, ensure_ascii=False, default=str)
        yield f"event: {event.event}\ndata: {payload}\n\n"
