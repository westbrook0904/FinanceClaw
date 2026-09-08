"""Artifact 存储后端抽象与实现。

位于 artifacts 模块的存储层：定义 ArtifactStore 协议，并提供本地文件、内存与
S3 兼容三种实现；所有存储键以租户/主体的哈希摘要构造，不暴露真实身份。
"""

import base64
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol


class ArtifactStore(Protocol):
    """Artifact 存储后端协议，定义内容字节的写入、读取与健康检查能力。

    使用场景：由 ArtifactService 依赖注入使用；实现必须以租户/主体的哈希摘要
    作为存储键的一部分并支持服务端加密描述，按结构化子类型满足本协议即可。
    """

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        """写入工件内容，返回可用于读取的存储 URI。

        Args:
            artifact_id: 工件唯一标识。
            content: 工件内容字节。
            tenant_id: 租户标识，参与存储键生成。
            subject_id: 主体标识，参与存储键生成。

        Returns:
            指向工件内容的存储 URI。

        """
        ...

    def get(self, storage_uri: str) -> bytes:
        """按存储 URI 读取工件内容字节。

        Args:
            storage_uri: ``put`` 返回的存储 URI。

        Returns:
            工件的原始内容字节。

        """
        ...

    def encryption_metadata(self) -> dict[str, str]:
        """返回当前存储后端的服务端加密描述，用于写入工件元数据。"""
        ...

    def health(self) -> bool:
        """检查存储后端是否可用，可用返回 True。"""
        ...


def _artifact_id(value: str) -> str:
    """校验工件标识的合法性，拒绝缺少前缀或含路径穿越的形式。

    Args:
        value: 待校验的工件标识。

    Returns:
        原样返回合法的工件标识。

    Raises:
        ValueError: 标识缺少 ``artifact-`` 前缀或包含路径分隔符、穿越片段时抛出。

    """
    if not value.startswith("artifact-") or "/" in value or ".." in value:
        raise ValueError("invalid artifact identifier")
    return value


def _scope(value: str) -> str:
    """把租户/主体标识哈希为定长摘要，避免存储键暴露真实身份。"""
    return sha256(value.encode()).hexdigest()[:32]


class LocalArtifactStore:
    """基于本地文件系统的 Artifact 存储实现，适用于开发与测试。

    使用场景：单机部署或测试环境中充当 Artifact Store；存储路径形如
    ``tenants/<租户摘要>/<主体摘要>/<工件标识>``，存储 URI 以
    ``artifact-local:`` 为前缀。

    Attributes:
        root: 存储根目录的绝对路径，构造时自动创建。

    """

    def __init__(self, root: str | Path) -> None:
        """创建本地存储并确保根目录存在。

        Args:
            root: 存储根目录，支持 ``~`` 展开的字符串或 ``Path``。

        """
        self.root = Path(root).expanduser().resolve()
        # 确保根目录存在，避免首次写入失败。
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        """把工件内容写入本地文件系统并返回存储 URI。

        Args:
            artifact_id: 工件唯一标识。
            content: 工件内容字节。
            tenant_id: 租户标识，参与存储路径生成。
            subject_id: 主体标识，参与存储路径生成。

        Returns:
            ``artifact-local:`` 前缀的存储 URI。

        Raises:
            ValueError: 工件标识非法时抛出。

        """
        # 1. 校验工件标识，防止路径注入。
        artifact_id = _artifact_id(artifact_id)
        # 2. 以租户/主体的哈希摘要构造隔离的存储路径。
        relative = Path("tenants") / _scope(tenant_id) / _scope(subject_id) / artifact_id
        path = self.root / relative
        # 3. 写入内容字节并返回可读取的存储 URI。
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"artifact-local:{relative.as_posix()}"

    def get(self, storage_uri: str) -> bytes:
        """按存储 URI 读取工件内容字节。

        Args:
            storage_uri: ``put`` 返回的 ``artifact-local:`` 前缀存储 URI。

        Returns:
            工件的原始内容字节。

        Raises:
            ValueError: URI 前缀不匹配、路径结构非法或指向租户命名空间之外时抛出。

        """
        prefix = "artifact-local:"
        if not storage_uri.startswith(prefix):
            raise ValueError("unsupported local artifact URI")
        # 校验路径结构：必须为租户命名空间下的相对路径且不含穿越片段。
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
        """返回服务端加密描述；本地实现不提供加密，返回空字典。"""
        return {}

    def health(self) -> bool:
        """检查存储根目录是否存在且可用，可用返回 True。"""
        return self.root.is_dir() and self.root.exists()


class InMemoryArtifactStore:
    """基于进程内字典的 Artifact 存储实现，仅用于测试。

    使用场景：单元测试中替代真实存储后端，避免文件与网络 IO；存储键形如
    ``<租户摘要>/<主体摘要>/<工件标识>``，存储 URI 以 ``artifact-memory:`` 为
    前缀。

    Attributes:
        values: 存储键到内容字节的映射，直接暴露以便测试断言。

    """

    def __init__(self) -> None:
        """创建空的内存存储。"""
        self.values: dict[str, bytes] = {}

    def put(
        self,
        artifact_id: str,
        content: bytes,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> str:
        """把工件内容写入内存字典并返回存储 URI。

        Args:
            artifact_id: 工件唯一标识。
            content: 工件内容字节。
            tenant_id: 租户标识，参与存储键生成。
            subject_id: 主体标识，参与存储键生成。

        Returns:
            ``artifact-memory:`` 前缀的存储 URI。

        Raises:
            ValueError: 工件标识非法时抛出。

        """
        # 以租户/主体的哈希摘要构造隔离的存储键。
        key = f"{_scope(tenant_id)}/{_scope(subject_id)}/{_artifact_id(artifact_id)}"
        self.values[key] = bytes(content)
        return f"artifact-memory:{key}"

    def get(self, storage_uri: str) -> bytes:
        """按存储 URI 读取内存中的工件内容字节。

        Args:
            storage_uri: ``put`` 返回的 ``artifact-memory:`` 前缀存储 URI。

        Returns:
            工件的原始内容字节。

        Raises:
            KeyError: 存储键不存在时抛出。

        """
        return self.values[storage_uri.removeprefix("artifact-memory:")]

    def encryption_metadata(self) -> dict[str, str]:
        """返回服务端加密描述；内存实现不提供加密，返回空字典。"""
        return {}

    def health(self) -> bool:
        """内存后端始终可用，返回 True。"""
        return True


class S3ArtifactStore:
    """基于 S3 兼容对象存储的 Artifact 存储实现，强制服务端加密（SSE）。

    使用场景：生产环境中持久化 Artifact 内容；写入时强制携带 SSE 参数并附带
    SHA256 校验和，存储键形如 ``<prefix>/tenants/<租户摘要>/subjects/<主体摘要>/
    <工件标识>``，不含真实租户与主体身份。

    Attributes:
        bucket: 目标存储桶名称。
        prefix: 所有存储键的公共前缀，默认 ``financeclaw``。
        sse_algorithm: 服务端加密算法，取值 ``AES256``、``aws:kms`` 或
            ``aws:kms:dsse``。
        kms_key_id: SSE-KMS 加密使用的 KMS 密钥标识，未启用时为 None。

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
        """创建 S3 存储后端并完成参数校验。

        Args:
            bucket: 目标存储桶名称，必须非空且不含斜杠。
            prefix: 所有存储键的公共前缀。
            endpoint_url: 自定义 S3 兼容服务端点，为 None 时使用默认端点。
            region_name: 服务端所在区域。
            sse_algorithm: 服务端加密算法，支持 ``AES256``、``aws:kms`` 与
                ``aws:kms:dsse``。
            kms_key_id: SSE-KMS 加密的 KMS 密钥标识，仅在 KMS 算法下允许提供。
            timeout_seconds: 客户端连接与读取超时（秒）。
            max_pool_connections: 客户端连接池最大连接数。
            client: 预构造的 S3 客户端（测试注入用），为 None 时按参数构建。

        Raises:
            ValueError: 存储桶名称非法、加密算法不受支持或 KMS 密钥与算法不匹配时抛出。

        """
        # 1. 校验桶名、SSE 算法与 KMS 密钥参数的一致性。
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
        # 2. 未注入客户端时，按超时、连接池与重试策略构建 boto3 客户端。
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
        """把工件内容上传到 S3 并返回存储 URI。

        Args:
            artifact_id: 工件唯一标识。
            content: 工件内容字节。
            tenant_id: 租户标识，仅以哈希摘要进入存储键。
            subject_id: 主体标识，仅以哈希摘要进入存储键。

        Returns:
            ``artifact-s3://<bucket>/<key>`` 形式的存储 URI。

        Raises:
            ValueError: 工件标识非法时抛出。

        """
        # 1. 校验工件标识，防止路径注入。
        artifact_id = _artifact_id(artifact_id)
        # 2. 组装租户命名空间下的存储键，租户/主体仅以哈希摘要出现。
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
        # 3. 构造上传请求：强制服务端加密并附带 SHA256 校验和。
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
        # 4. 上传对象并返回存储 URI。
        self._client.put_object(**request)
        return f"artifact-s3://{self.bucket}/{key}"

    def get(self, storage_uri: str) -> bytes:
        """按存储 URI 从 S3 读取工件内容字节。

        Args:
            storage_uri: ``put`` 返回的 ``artifact-s3://`` 前缀存储 URI。

        Returns:
            工件的原始内容字节。

        Raises:
            ValueError: URI 不属于当前配置的存储桶，或存储键位于租户命名空间
                之外、包含路径穿越片段时抛出。

        """
        prefix = f"artifact-s3://{self.bucket}/"
        # 1. 校验 URI 属于当前配置的存储桶。
        if not storage_uri.startswith(prefix):
            raise ValueError("artifact URI does not belong to the configured bucket")
        key = storage_uri.removeprefix(prefix)
        # 2. 校验存储键位于本前缀的租户命名空间内，且不含路径穿越片段。
        required_prefix = f"{self.prefix}/tenants/" if self.prefix else "tenants/"
        if not key.startswith(required_prefix) or "/../" in f"/{key}/":
            raise ValueError("artifact key is outside the tenant namespace")
        # 3. 拉取对象内容并读取全部字节。
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    def encryption_metadata(self) -> dict[str, str]:
        """返回写入工件时使用的服务端加密描述（SSE 算法与可选 KMS 密钥）。"""
        metadata = {"server_side_encryption": self.sse_algorithm}
        if self.kms_key_id:
            metadata["kms_key_id"] = self.kms_key_id
        return metadata

    def health(self) -> bool:
        """通过 ``head_bucket`` 探测存储桶可达性，不可达返回 False。"""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            return False
        return True
