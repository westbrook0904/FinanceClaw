"""实现本地文件与 S3 兼容对象存储的制品读写边界。"""

import base64
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol


class ArtifactStore(Protocol):
    """定义制品Store。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        """写入制品二进制内容，并返回不含凭证的存储 URI。"""
        ...

    def get(self, storage_uri: str) -> bytes:
        """按标识读取制品Store；不存在时由下层仓储抛出明确异常。"""
        ...

    def encryption_metadata(self) -> dict[str, str]:
        """返回可安全持久化的静态加密算法元数据。"""
        ...

    def health(self) -> bool:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
        ...


def _artifact_id(value: str) -> str:
    """根据租户、主体和内容哈希生成确定性制品标识。"""
    if not value.startswith("artifact-") or "/" in value or ".." in value:
        raise ValueError("invalid artifact identifier")
    return value


def _scope(value: str) -> str:
    """把租户与主体规范化为安全目录片段，防止路径穿越。"""
    return sha256(value.encode()).hexdigest()[:32]


class LocalArtifactStore:
    """定义Local制品Store。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        root: 本地制品存储的受控根目录。
    """

    def __init__(self, root: str | Path) -> None:
        """注入并保存Local制品Store所需的协作对象，同时校验构造期不变量。"""
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
        """写入制品二进制内容，并返回不含凭证的存储 URI。"""
        artifact_id = _artifact_id(artifact_id)
        relative = Path("tenants") / _scope(tenant_id) / _scope(subject_id) / artifact_id
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"artifact-local:{relative.as_posix()}"

    def get(self, storage_uri: str) -> bytes:
        """按标识读取Local制品Store；不存在时由下层仓储抛出明确异常。"""
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
        """返回可安全持久化的静态加密算法元数据。"""
        return {}

    def health(self) -> bool:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
        return self.root.is_dir() and self.root.exists()


class InMemoryArtifactStore:
    """定义In记忆制品Store。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        values: 目录初始化时接收的版本化配置或工具集合。
    """

    def __init__(self) -> None:
        """注入并保存In记忆制品Store所需的协作对象，同时校验构造期不变量。"""
        self.values: dict[str, bytes] = {}

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        """写入制品二进制内容，并返回不含凭证的存储 URI。"""
        key = f"{_scope(tenant_id)}/{_scope(subject_id)}/{_artifact_id(artifact_id)}"
        self.values[key] = bytes(content)
        return f"artifact-memory:{key}"

    def get(self, storage_uri: str) -> bytes:
        """按标识读取In记忆制品Store；不存在时由下层仓储抛出明确异常。"""
        return self.values[storage_uri.removeprefix("artifact-memory:")]

    def encryption_metadata(self) -> dict[str, str]:
        """返回可安全持久化的静态加密算法元数据。"""
        return {}

    def health(self) -> bool:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
        return True


class S3ArtifactStore:
    """定义S3制品Store。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        bucket: S3 兼容对象存储桶名称。
        prefix: 写入对象键时统一添加的目录前缀。
        sse_algorithm: 对象存储服务端加密算法。
        kms_key_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        _client: 负责与外部 Agent Server 或供应商通信的端口实现。
    """

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
        """注入并保存S3制品Store所需的协作对象，同时校验构造期不变量。"""
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
        """写入制品二进制内容，并返回不含凭证的存储 URI。"""
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
        """按标识读取S3制品Store；不存在时由下层仓储抛出明确异常。"""
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
        """返回可安全持久化的静态加密算法元数据。"""
        metadata = {"server_side_encryption": self.sse_algorithm}
        if self.kms_key_id:
            metadata["kms_key_id"] = self.kms_key_id
        return metadata

    def health(self) -> bool:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            return False
        return True
