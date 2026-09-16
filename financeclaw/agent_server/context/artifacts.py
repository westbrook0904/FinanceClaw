"""工具结果的统一归档；与工具名称、MCP 注解和 Skill 文档无关。"""

import json
from hashlib import sha256

from langchain_core.messages import ToolMessage

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.artifacts.views import (
    VIEW_KEY,
    archive_payload,
    business_view,
    encode,
    reference_view,
)
from financeclaw.shared.skills.access import ACCESS_KEY, RESOURCE_KEY

SOURCE_KEY = "financeclaw_source"


class ToolResultArchive:
    """先归档再清理，保留可以按 Turn 找回的结果快照。"""

    def __init__(self, service: ArtifactService) -> None:
        """使用平台已有工件服务持久化结果快照。"""
        self.service = service

    def save(self, message: ToolMessage, context: ExecutionContext) -> dict:
        """幂等归档一条结果；已有平台工件引用时直接复用。"""
        reference = message.additional_kwargs.get("artifact_ref")
        if isinstance(reference, dict):
            return reference
        source = message.additional_kwargs.get(SOURCE_KEY, {})
        owner = context.model_copy(
            update={
                "conversation_id": source.get("conversation_id", context.conversation_id),
                "turn_id": source.get("turn_id", context.turn_id),
            }
        )
        options = message.additional_kwargs.get(VIEW_KEY, {})
        payload = archive_payload(
            message.content,
            message.artifact,
            name=message.name,
            status=message.status,
            mcp=options.get("mcp", False),
        )
        content_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        metadata = self.service.persist(
            payload,
            skill_access_refs=tuple(message.additional_kwargs.get(ACCESS_KEY, [])),
            context=owner,
            source_type="tool_result",
            source_id=message.tool_call_id,
            idempotency_key=f"{owner.turn_id}:{message.tool_call_id}:{content_hash}",
            **(
                {
                    "memory_privacy_epoch": message.additional_kwargs["memory_privacy_epoch"],
                    "memory_references": tuple(
                        message.additional_kwargs.get("financeclaw_memory_refs", ())
                    ),
                }
                if message.additional_kwargs.get("memory_derived")
                else {}
            ),
        )
        base = {
            "artifact_id": metadata.artifact_id,
            "content_hash": metadata.content_hash,
            "size_bytes": metadata.size_bytes,
            "source_turn_id": owner.turn_id,
        }
        kind, data = business_view(payload)
        return reference_view(base, kind, data, options.get("rule"))

    def project(self, message: ToolMessage, context: ExecutionContext) -> ToolMessage:
        """生成可回读的短消息，不重跑原工具。"""
        reference = self.save(message, context)
        return message.model_copy(
            update={
                "content": encode(
                    {"historical_tool_result": True, **reference, "read_with": "read_artifact"},
                ),
                "artifact": reference,
                "additional_kwargs": {
                    **{k: v for k, v in message.additional_kwargs.items() if k != RESOURCE_KEY},
                    "artifact_ref": reference,
                },
            }
        )
