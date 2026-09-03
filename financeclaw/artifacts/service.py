"""Large-result offload and owner-scoped retrieval."""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

from financeclaw.contracts import ExecutionContext

from .models import ArtifactMetadata
from .repository import ArtifactNotFound, ArtifactRepository
from .storage import ArtifactStore


class ArtifactService:
    def __init__(
        self,
        repository: ArtifactRepository,
        store: ArtifactStore,
        *,
        inline_bytes: int = 16_384,
    ) -> None:
        if inline_bytes < 256:
            raise ValueError("artifact inline threshold must be at least 256 bytes")
        self.repository = repository
        self.store = store
        self.inline_bytes = inline_bytes

    def offload(
        self,
        value: Any,
        *,
        context: ExecutionContext,
        source_type: str,
        source_id: str,
        content_type: str = "application/json",
    ) -> tuple[Any, ArtifactMetadata | None]:
        serialized = _serialize(value)
        payload = serialized.encode()
        if len(payload) <= self.inline_bytes:
            return value, None
        artifact_id = f"artifact-{uuid4().hex}"
        storage_uri = self.store.put(artifact_id, payload)
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            content_type=content_type,
            storage_uri=storage_uri,
            content_hash=sha256(payload).hexdigest(),
            size_bytes=len(payload),
            source_type=source_type,
            source_id=source_id,
            access_policy={"required_scope": "artifacts:read"},
        )
        self.repository.save(metadata)
        bounded = {
            "summary": serialized[:480] + ("…" if len(serialized) > 480 else ""),
            "artifact_id": artifact_id,
            "content_hash": metadata.content_hash,
            "size_bytes": metadata.size_bytes,
            "source": source_id,
            "historical_or_large_result": True,
        }
        bounded.update(_provenance(value))
        return json.dumps(bounded, ensure_ascii=False, sort_keys=True), metadata

    def persist(
        self,
        value: Any,
        *,
        context: ExecutionContext,
        source_type: str,
        source_id: str,
        idempotency_key: str,
        content_type: str = "application/json",
    ) -> ArtifactMetadata:
        """Persist a report exactly once for a stable workflow node key.

        Unlike ``offload``, published reports are artifacts even when small.
        The deterministic ID lets checkpoint replay return the original
        artifact instead of creating a second externally visible report.
        """

        serialized = _serialize(value)
        payload = serialized.encode()
        content_hash = sha256(payload).hexdigest()
        identity = json.dumps(
            {
                "tenant_id": context.tenant_id,
                "subject_id": context.subject_id,
                "source_type": source_type,
                "source_id": source_id,
                "idempotency_key": idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact_id = f"artifact-{sha256(identity.encode()).hexdigest()}"
        try:
            existing = self.repository.get_owned(artifact_id, context.tenant_id, context.subject_id)
        except ArtifactNotFound:
            existing = None
        if existing is not None:
            if (
                existing.content_hash != content_hash
                or existing.source_type != source_type
                or existing.source_id != source_id
            ):
                raise ValueError("artifact idempotency key identifies different content")
            return existing

        storage_uri = self.store.put(artifact_id, payload)
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            content_type=content_type,
            storage_uri=storage_uri,
            content_hash=content_hash,
            size_bytes=len(payload),
            source_type=source_type,
            source_id=source_id,
            access_policy={"required_scope": "artifacts:read"},
        )
        return self.repository.save(metadata)

    def read(self, artifact_id: str, *, context: ExecutionContext) -> bytes:
        if "*" not in context.scopes and "artifacts:read" not in context.scopes:
            raise PermissionError("artifacts:read scope is required")
        metadata = self.repository.get_owned(artifact_id, context.tenant_id, context.subject_id)
        payload = self.store.get(metadata.storage_uri)
        if sha256(payload).hexdigest() != metadata.content_hash:
            raise ValueError("artifact content hash mismatch")
        return payload


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _provenance(value: Any) -> dict[str, Any]:
    candidate = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            return {}
    if not isinstance(candidate, dict):
        return {}
    provenance: dict[str, Any] = {}
    source = candidate.get("provider", candidate.get("source"))
    if isinstance(source, str):
        provenance["source"] = source
    if isinstance(candidate.get("as_of"), str):
        provenance["as_of"] = candidate["as_of"]
    return provenance
