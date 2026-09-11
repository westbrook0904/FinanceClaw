"""Cancellation boundaries for short synchronous database transactions."""

import asyncio
from collections.abc import Callable
from typing import Any


async def run_sync[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Drain an in-flight transaction before propagating cancellation to resource shutdown."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
