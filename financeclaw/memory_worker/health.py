"""Read the dedicated process heartbeat without opening an HTTP health server."""

import json
import time
from pathlib import Path

HEALTH_PATH = Path("/tmp/financeclaw-memory-worker-health.json")


def check():
    """Fail on stale, malformed or explicitly unhealthy consumer status."""
    try:
        value = json.loads(HEALTH_PATH.read_text())
        valid = 0 <= time.time() - value["time"] < 20 and value["healthy"] is True
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    raise SystemExit(0 if valid else 1)


if __name__ == "__main__":
    check()
