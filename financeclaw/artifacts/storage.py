"""Content storage ports and bounded local development implementation."""

from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    def put(self, artifact_id: str, content: bytes) -> str: ...

    def get(self, storage_uri: str) -> bytes: ...


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, artifact_id: str, content: bytes) -> str:
        if not artifact_id.startswith("artifact-") or "/" in artifact_id:
            raise ValueError("invalid artifact identifier")
        path = self.root / artifact_id
        path.write_bytes(content)
        return f"artifact-local:{artifact_id}"

    def get(self, storage_uri: str) -> bytes:
        prefix = "artifact-local:"
        if not storage_uri.startswith(prefix):
            raise ValueError("unsupported local artifact URI")
        artifact_id = storage_uri.removeprefix(prefix)
        if not artifact_id.startswith("artifact-") or "/" in artifact_id:
            raise ValueError("invalid artifact URI")
        return (self.root / artifact_id).read_bytes()


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(self, artifact_id: str, content: bytes) -> str:
        self.values[artifact_id] = bytes(content)
        return f"artifact-memory:{artifact_id}"

    def get(self, storage_uri: str) -> bytes:
        return self.values[storage_uri.removeprefix("artifact-memory:")]
