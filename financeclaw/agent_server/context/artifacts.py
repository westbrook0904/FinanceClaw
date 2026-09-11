"""工具结果的统一归档；与工具名称、MCP 注解和 Skill 文档无关。"""

import json
from hashlib import sha256

from langchain_core.messages import ToolMessage

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.artifacts.service import ArtifactService

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
                "run_id": source.get("run_id", context.run_id),
            }
        )
        payload = {
            "content": message.content,
            "artifact": message.artifact,
            "name": message.name,
            "status": message.status,
        }
        content_hash = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        metadata = self.service.persist(
            payload,
            context=owner,
            source_type="tool_result",
            source_id=message.tool_call_id,
            idempotency_key=f"{owner.turn_id}:{message.tool_call_id}:{content_hash}",
        )
        return {
            "artifact_id": metadata.artifact_id,
            "content_hash": metadata.content_hash,
            "size_bytes": metadata.size_bytes,
            "source_turn_id": owner.turn_id,
        }

    def project(self, message: ToolMessage, context: ExecutionContext) -> ToolMessage:
        """生成可回读的短消息，不重跑原工具。"""
        reference = self.save(message, context)
        return message.model_copy(
            update={
                "content": json.dumps(
                    {"historical_tool_result": True, **reference, "read_with": "read_artifact"},
                    ensure_ascii=False,
                ),
                "artifact": reference,
                "additional_kwargs": {**message.additional_kwargs, "artifact_ref": reference},
            }
        )
