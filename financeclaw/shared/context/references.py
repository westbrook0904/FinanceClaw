"""将明确授权且固定内容版本的业务引用解析成有界子任务输入。"""

import re
from hashlib import sha256
from typing import Any

from financeclaw.kernel.context import DataClassification, ExecutionContext
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.conversation.repository import ConversationRepository

_REFERENCE = re.compile(r"^(message|artifact):([A-Za-z0-9._:-]{1,128})@([0-9a-f]{64})$")
_CLASSIFICATION = {value: index for index, value in enumerate(DataClassification)}


def resolve_context_refs(
    refs: tuple[str, ...],
    *,
    context: ExecutionContext,
    conversations: ConversationRepository | None,
    artifacts: ArtifactService | None,
    max_bytes: int = 32_768,
) -> list[dict[str, Any]]:
    """仅支持 message:id@sha256 和 artifact:id@sha256，拒绝 URL 与任意路径。

    归属、显式会话范围、内容版本、数据分级与总大小全部校验后，内容才能下发。
    引用本身不是工具授权，artifact 的读取仍使用原权限中的 artifacts:read。
    """
    resolved = []
    total = 0
    for ref in refs:
        match = _REFERENCE.fullmatch(ref)
        if match is None:
            raise ValueError("context reference must be a supported ID with an explicit SHA-256")
        kind, identifier, expected_hash = match.groups()
        classification = DataClassification.INTERNAL
        if kind == "message":
            if conversations is None or context.conversation_id is None:
                raise ValueError("message reference resolver is unavailable")
            conversations.get_owned(context.conversation_id, context.tenant_id, context.subject_id)
            message = next(
                (
                    item
                    for item in conversations.list_messages(context.conversation_id)
                    if item.message_id == identifier and item.visible
                ),
                None,
            )
            if message is None:
                raise ValueError("message reference is outside the authorized conversation")
            content = message.content
            source = {
                "conversation_id": message.conversation_id,
                "turn_id": message.turn_id,
                "role": message.role.value,
                "sequence": message.sequence,
            }
        else:
            if artifacts is None:
                raise ValueError("artifact reference resolver is unavailable")
            metadata = artifacts.repository.get_owned(
                identifier, context.tenant_id, context.subject_id
            )
            if metadata.size_bytes > max_bytes - total:
                raise ValueError("worker context references exceed the size budget")
            classification = DataClassification(metadata.access_policy.get("data_classification"))
            if _CLASSIFICATION[classification] > _CLASSIFICATION[context.data_classification]:
                raise PermissionError("reference classification exceeds the execution snapshot")
            if metadata.content_type not in {"text/plain", "application/json", "text/markdown"}:
                raise ValueError("worker context supports only textual artifacts")
            content = artifacts.read(identifier, context=context).decode("utf-8")
            source = {"source_type": metadata.source_type, "source_id": metadata.source_id}
        if _CLASSIFICATION[classification] > _CLASSIFICATION[context.data_classification]:
            raise PermissionError("reference classification exceeds the execution snapshot")
        encoded = content.encode("utf-8")
        if sha256(encoded).hexdigest() != expected_hash:
            raise ValueError("context reference content version changed")
        total += len(encoded)
        if total > max_bytes:
            raise ValueError("worker context references exceed the size budget")
        resolved.append(
            {
                "ref": ref,
                "kind": kind,
                "content": content,
                "content_hash": expected_hash,
                "source": source,
                "data_classification": classification.value,
            }
        )
    return resolved
