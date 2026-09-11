"""Compile the single LangGraph deployment configuration into official runtime variables."""

import json
import shlex
from pathlib import Path


def configure():
    """Resolve callable paths at build time without importing graph or application modules."""
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "langgraph.json").read_text())
    values = {
        "LANGSERVE_GRAPHS": config["graphs"],
        "LANGGRAPH_HTTP": config["http"],
        "LANGGRAPH_AUTH": config["auth"],
        "LANGGRAPH_STORE": config["store"],
    }

    def resolve(value):
        """Replace local callable paths while preserving all native configuration fields."""
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, str) and value.startswith("./"):
            return str(root / value[2:])
        return value

    lines = [
        "export " + key + "=" + shlex.quote(json.dumps(resolve(value)))
        for key, value in values.items()
    ]
    (root / "native-env.sh").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    configure()
