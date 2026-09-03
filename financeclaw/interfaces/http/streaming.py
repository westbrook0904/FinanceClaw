"""SSE（Server-Sent Events）流式输出适配：把内核事件流序列化为 SSE 文本帧。

本模块属于 interfaces（HTTP 协议适配层），只做传输格式转换，
不掺入任何业务规则或状态。
"""

import json
from collections.abc import AsyncIterator

from financeclaw.kernel import StreamEvent


async def project_sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """把内核 ``StreamEvent`` 异步流逐条投影为 SSE 文本帧流。

    使用场景：FastAPI 的 ``StreamingResponse`` 以 ``text/event-stream``
    媒体类型消费本生成器，向客户端持续下发运行事件。

    Args:
        events: 内核 ``StreamEvent`` 异步迭代器，每项含事件名与负载。

    Yields:
        单条事件的 SSE 文本帧：首行为 ``event: <事件名>``，次行为
        ``data: <JSON 负载>``，随后以一个空行结尾；负载以 UTF-8 JSON
        编码，不可序列化的对象退化为字符串。

    """
    async for event in events:
        payload = json.dumps(event.data, ensure_ascii=False, default=str)
        yield f"event: {event.event}\ndata: {payload}\n\n"
