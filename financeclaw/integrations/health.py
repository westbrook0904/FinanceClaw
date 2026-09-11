"""Process-local integration health, without another public HTTP service."""

import asyncio
import json
import time
from pathlib import Path

HEALTH_PATH = Path("/tmp/financeclaw-integrations-health.json")


async def heartbeat(consumer, stop, *, channel=None, notifications=None):
    """Report consumer health with a heartbeat that expires after process failure."""
    while not stop.is_set():
        checks = {
            "history": not consumer.done(),
            "channel": await channel.health() if channel else True,
            "notifications": bool(notifications.task and not notifications.task.done())
            if notifications
            else True,
        }
        temporary = HEALTH_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps({"at": time.time(), "checks": checks}))
        temporary.replace(HEALTH_PATH)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


def check():
    """Exit unsuccessfully if any consumer is unhealthy or the heartbeat is stale."""
    try:
        value = json.loads(HEALTH_PATH.read_text())
        valid = time.time() - value["at"] < 20 and all(value["checks"].values())
    except (OSError, ValueError, KeyError):
        valid = False
    raise SystemExit(0 if valid else 1)


if __name__ == "__main__":
    check()
