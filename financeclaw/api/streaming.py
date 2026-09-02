"""Safe product SSE projection for Agent Server stream parts."""

import json
from collections.abc import AsyncIterator

from financeclaw.contracts import StreamEvent


async def project_sse(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    async for event in events:
        payload = json.dumps(event.data, ensure_ascii=False, default=str)
        yield f"event: {event.event}\ndata: {payload}\n\n"
