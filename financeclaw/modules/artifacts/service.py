"""Artifact 的应用服务层。

位于 artifacts 模块的服务层：把超大工具结果 offload 为 Artifact 并返回有界摘要，
支持按幂等键的确定性持久化，读取时强制校验权限范围、归属与内容哈希。
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

from financeclaw.kernel import ExecutionContext

from .models import ArtifactMetadata
from .repository import ArtifactNotFound, ArtifactRepository
from .storage import ArtifactStore


class ArtifactService:
    """负责工具结果 offload、幂等持久化与安全读取的应用服务。

    使用场景：工具返回内容超过内联阈值时由 ``offload`` 落盘并返回有界摘要；
    需要幂等写入时用 ``persist`` 按幂等键去重；读取 Artifact 时经 ``read``
    强制校验 ``artifacts:read`` 权限范围、tenant/subject 归属与内容哈希。

    Attributes:
        repository: Artifact 元数据仓储，负责元数据的持久化与归属查询。
        store: Artifact 存储后端，负责内容字节的写入与读取。
        inline_bytes: 内联阈值（字节），内容不超过该值时不生成 Artifact。

    """

    def __init__(
        self,
        repository: ArtifactRepository,
        store: ArtifactStore,
        *,
        inline_bytes: int = 16_384,
    ) -> None:
        """创建服务实例并校验内联阈值。

        Args:
            repository: Artifact 元数据仓储。
            store: Artifact 存储后端。
            inline_bytes: 内联阈值（字节），默认 16384，必须不小于 256。

        Raises:
            ValueError: 内联阈值小于 256 字节时抛出。

        """
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
        """把工具结果 offload 为 Artifact，返回有界负载与工件元数据。

        内容序列化后不超过内联阈值时原样返回、不生成 Artifact；超过时写入
        Artifact Store 并落库元数据，返回截断摘要与 Artifact 引用信息。

        Args:
            value: 待 offload 的工具结果，支持 JSON 可序列化对象与 Pydantic 模型。
            context: 当前执行上下文，提供租户、主体等归属信息。
            source_type: 产生结果的来源类型（如工具类型），用于溯源。
            source_id: 产生结果的来源标识（如工具调用标识），用于溯源。
            content_type: 工件内容的 MIME 类型，默认 ``application/json``。

        Returns:
            二元组 ``(bounded, metadata)``：``bounded`` 为原始结果（内联时）或包含
            截断摘要、工件标识与溯源信息的 JSON 字符串；``metadata`` 为新落库的
            工件元数据，未生成 Artifact 时为 None。

        """
        # 1. 序列化内容并判断是否超过内联阈值，未超过则原样返回。
        serialized = _serialize(value)
        payload = serialized.encode()
        if len(payload) <= self.inline_bytes:
            return value, None
        # 2. 生成全局唯一工件标识，并把内容字节写入 Artifact Store。
        artifact_id = f"artifact-{uuid4().hex}"
        storage_uri = self.store.put(
            artifact_id,
            payload,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
        )
        # 3. 构造元数据（含 SHA256 摘要、访问策略与加密描述）并持久化。
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
            encryption_metadata=self.store.encryption_metadata(),
        )
        self.repository.save(metadata)
        # 4. 生成有界摘要（截断至 480 字符）并补充来源等溯源字段。
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
        """按幂等键持久化工具结果，重复提交相同内容时返回既有元数据。

        工件标识由租户、主体、来源与幂等键共同推导，天然幂等：若同标识工件已
        存在且内容一致则直接复用；内容或来源不一致则抛出异常，防止幂等键被误用。

        Args:
            value: 待持久化的工具结果。
            context: 当前执行上下文，提供租户、主体等归属信息。
            source_type: 来源类型，参与幂等标识推导与溯源。
            source_id: 来源标识，参与幂等标识推导与溯源。
            idempotency_key: 调用方提供的幂等键，相同键必须对应相同内容。
            content_type: 工件内容的 MIME 类型，默认 ``application/json``。

        Returns:
            幂等命中的既有元数据，或新写入的工件元数据。

        Raises:
            ValueError: 幂等键已存在但对应内容或来源不一致时抛出。

        """
        # 1. 序列化内容并计算 SHA256 摘要。
        serialized = _serialize(value)
        payload = serialized.encode()
        content_hash = sha256(payload).hexdigest()
        # 2. 用归属、来源与幂等键推导确定性的工件标识，保证幂等。
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
        # 3. 查询同标识工件；命中则校验内容与来源一致后复用既有记录。
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

        # 4. 未命中时写入存储并落库新元数据。
        storage_uri = self.store.put(
            artifact_id,
            payload,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
        )
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
            encryption_metadata=self.store.encryption_metadata(),
        )
        return self.repository.save(metadata)

    def read(self, artifact_id: str, *, context: ExecutionContext) -> bytes:
        """读取 Artifact 内容字节，读取前强制校验权限、归属与完整性。

        Args:
            artifact_id: 工件唯一标识。
            context: 当前执行上下文，用于权限范围与归属校验。

        Returns:
            工件的原始内容字节。

        Raises:
            PermissionError: 调用方缺少 ``artifacts:read`` 权限范围时抛出。
            ArtifactNotFound: 工件不存在或不属于当前租户/主体时抛出。
            ValueError: 内容 SHA256 与元数据不一致（内容被篡改或损坏）时抛出。

        """
        # 1. 校验调用方具备 artifacts:read 权限范围（持有通配符亦可）。
        if "*" not in context.scopes and "artifacts:read" not in context.scopes:
            raise PermissionError("artifacts:read scope is required")
        # 2. 按归属读取元数据，确保 owner 隔离。
        metadata = self.repository.get_owned(artifact_id, context.tenant_id, context.subject_id)
        # 3. 读取内容并校验 SHA256，防止存储侧内容损坏或被篡改。
        payload = self.store.get(metadata.storage_uri)
        if sha256(payload).hexdigest() != metadata.content_hash:
            raise ValueError("artifact content hash mismatch")
        return payload


def _serialize(value: Any) -> str:
    """把任意工具结果序列化为 JSON 字符串，字符串输入原样返回。"""
    if isinstance(value, str):
        return value
    # Pydantic 模型先转为 JSON 兼容字典，再统一序列化。
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _provenance(value: Any) -> dict[str, Any]:
    """从工具结果中提取 ``source``（provider）与 ``as_of`` 溯源字段。

    Args:
        value: 原始工具结果。

    Returns:
        提取到的溯源键值；结果无法解析为对象或缺少相应字段时返回空字典。

    """
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
