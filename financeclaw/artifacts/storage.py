"""Tenant-scoped local and S3-compatible content storage adapters."""

import base64
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol


class ArtifactStore(Protocol):
    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str: ...

    def get(self, storage_uri: str) -> bytes: ...

    def encryption_metadata(self) -> dict[str, str]: ...

    def health(self) -> bool: ...


def _artifact_id(value: str) -> str:
    if not value.startswith("artifact-") or "/" in value or ".." in value:
        raise ValueError("invalid artifact identifier")
    return value


def _scope(value: str) -> str:
    """Avoid exposing tenant identifiers in bucket keys and local paths."""

    return sha256(value.encode()).hexdigest()[:32]


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        artifact_id = _artifact_id(artifact_id)
        relative = Path("tenants") / _scope(tenant_id) / _scope(subject_id) / artifact_id
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"artifact-local:{relative.as_posix()}"

    def get(self, storage_uri: str) -> bytes:
        prefix = "artifact-local:"
        if not storage_uri.startswith(prefix):
            raise ValueError("unsupported local artifact URI")
        relative = Path(storage_uri.removeprefix(prefix))
        if (
            relative.is_absolute()
            or len(relative.parts) != 4
            or relative.parts[0] != "tenants"
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("invalid artifact URI")
        _artifact_id(relative.parts[-1])
        return (self.root / relative).read_bytes()

    def encryption_metadata(self) -> dict[str, str]:
        return {}

    def health(self) -> bool:
        return self.root.is_dir() and self.root.exists()


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        key = f"{_scope(tenant_id)}/{_scope(subject_id)}/{_artifact_id(artifact_id)}"
        self.values[key] = bytes(content)
        return f"artifact-memory:{key}"

    def get(self, storage_uri: str) -> bytes:
        return self.values[storage_uri.removeprefix("artifact-memory:")]

    def encryption_metadata(self) -> dict[str, str]:
        return {}

    def health(self) -> bool:
        return True


class S3ArtifactStore:
    """Store private tenant-scoped objects with mandatory server-side encryption."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "financeclaw",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        sse_algorithm: str = "AES256",
        kms_key_id: str | None = None,
        timeout_seconds: float = 10.0,
        max_pool_connections: int = 50,
        client: Any | None = None,
    ) -> None:
        if not bucket or "/" in bucket:
            raise ValueError("invalid artifact bucket")
        if sse_algorithm not in {"AES256", "aws:kms", "aws:kms:dsse"}:
            raise ValueError("unsupported S3 encryption")
        if kms_key_id and not sse_algorithm.startswith("aws:kms"):
            raise ValueError("KMS key requires aws:kms encryption")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.sse_algorithm = sse_algorithm
        self.kms_key_id = kms_key_id
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region_name,
                config=Config(
                    connect_timeout=timeout_seconds,
                    read_timeout=timeout_seconds,
                    max_pool_connections=max_pool_connections,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        self._client = client

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        artifact_id = _artifact_id(artifact_id)
        key = "/".join(
            filter(
                None,
                (
                    self.prefix,
                    "tenants",
                    _scope(tenant_id),
                    "subjects",
                    _scope(subject_id),
                    artifact_id,
                ),
            )
        )
        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": content,
            "ContentLength": len(content),
            "ChecksumSHA256": base64.b64encode(sha256(content).digest()).decode(),
            "ServerSideEncryption": self.sse_algorithm,
        }
        if self.kms_key_id:
            request["SSEKMSKeyId"] = self.kms_key_id
        self._client.put_object(**request)
        return f"artifact-s3://{self.bucket}/{key}"

    def get(self, storage_uri: str) -> bytes:
        prefix = f"artifact-s3://{self.bucket}/"
        if not storage_uri.startswith(prefix):
            raise ValueError("artifact URI does not belong to the configured bucket")
        key = storage_uri.removeprefix(prefix)
        required_prefix = f"{self.prefix}/tenants/" if self.prefix else "tenants/"
        if not key.startswith(required_prefix) or "/../" in f"/{key}/":
            raise ValueError("artifact key is outside the tenant namespace")
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    def encryption_metadata(self) -> dict[str, str]:
        metadata = {"server_side_encryption": self.sse_algorithm}
        if self.kms_key_id:
            metadata["kms_key_id"] = self.kms_key_id
        return metadata

    def health(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            return False
        return True
