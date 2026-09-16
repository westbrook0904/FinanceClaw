"""历史搜索索引与精确回读；Journal/Artifact 保持原文权威。"""

import json
from datetime import UTC, datetime

from financeclaw.shared.artifacts.views import business_view, read_view
from financeclaw.shared.memory.namespace import owner_namespace
from financeclaw.shared.memory.observability import store_operation
from financeclaw.shared.skills.access import ACCESS_KEY, merge_access, require_access


class HistoryService:
    """统一有界历史入口，索引命中后复验实际来源。"""

    def __init__(self, conversations, artifacts):
        """保存历史原文及工件读取所需的服务。"""
        self.conversations = conversations
        self.artifacts = artifacts

    def privacy_epoch(self, context):
        """在派生历史内容读取前固定隐私版本，完成后再次核对。"""
        return self.artifacts.memory_privacy_epoch(context)

    def result_provenance(
        self, context, result, *, tool_name, initial_epoch, skill_authorizer=None
    ):
        """历史派生文本保留版本；业务原始工件仍按其原始访问策略处理。"""
        if self.privacy_epoch(context) != initial_epoch:
            raise PermissionError("privacy changed while reading historical context")
        if tool_name == "read_artifact":
            metadata = self.artifacts.repository.get_owned(
                result["artifact_id"], context.tenant_id, context.subject_id
            )
            self.artifacts.validate_memory_access(metadata, context)
            refs = require_access(
                metadata.access_policy.get("skill_access_refs", []), skill_authorizer
            )
            provenance = {ACCESS_KEY: refs} if refs else {}
            epoch = metadata.access_policy.get("memory_privacy_epoch")
            if epoch is None:
                return provenance
            return {
                **provenance,
                "memory_derived": True,
                "memory_privacy_epoch": epoch,
                "financeclaw_memory_refs": metadata.access_policy.get("memory_refs", []),
            }
        refs = require_access(result.pop("_skill_access_refs", []), skill_authorizer)
        return {"memory_derived": True, "memory_privacy_epoch": initial_epoch, ACCESS_KEY: refs}

    @staticmethod
    def namespace(context, conversation_id=None):
        """跨会话检索也只允许当前主体自己的 history 前缀。"""
        base = (*owner_namespace(context), "history")
        return (*base, conversation_id) if conversation_id else base

    def _conversation(self, context, conversation_id):
        """解析明确的会话范围并校验读取权限和主体归属。"""
        if "*" not in context.scopes and "memory:read" not in context.scopes:
            raise PermissionError("memory:read scope is required")
        identity = conversation_id or context.conversation_id
        if not identity:
            raise ValueError("conversation_id is required")
        self.conversations.get_owned(identity, context.tenant_id, context.subject_id)
        return identity

    def search(
        self,
        context,
        store,
        query,
        *,
        conversation_id=None,
        across_conversations=False,
        limit=6,
        skill_authorizer=None,
    ):
        """原生语义搜索只定位来源；已删除或已变化的原文不返回。"""
        if not 1 <= limit <= 20 or not query or len(query) > 512:
            raise ValueError("invalid history query")
        current = self._conversation(context, conversation_id)
        if store is None:
            raise ValueError("LangGraph Store is unavailable")
        namespace = self.namespace(context, None if across_conversations else current)
        with store_operation("explicit_history_search", query_chars=len(query)):
            items = store.search(namespace, query=query, limit=min(60, limit * 3))
        matches, refs = [], []
        for item in items:
            value = item.value
            try:
                self._conversation(context, value["conversation_id"])
                source = self.conversations.get_message_owned(
                    value["message_id"],
                    context.tenant_id,
                    context.subject_id,
                )
            except LookupError:
                continue
            if (
                not source.visible
                or (not across_conversations and source.conversation_id != current)
                or source.conversation_id != value["conversation_id"]
                or source.content_hash != value["content_hash"]
                or source.turn_id != value["turn_id"]
            ):
                continue
            try:
                source_refs = require_access(source.skill_access_refs, skill_authorizer)
            except ValueError:
                continue
            refs = merge_access(refs, source_refs)
            start, end = value["start"], value["end"]
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(source.content)
            ):
                continue
            matches.append(
                {
                    "conversation_id": source.conversation_id,
                    "turn_id": source.turn_id,
                    "message_id": source.message_id,
                    "content_hash": source.content_hash,
                    "excerpt": source.content[start : min(end, start + 1000)],
                    "offset": start,
                    "score": item.score,
                    "historical": True,
                }
            )
            if len(matches) == limit:
                break
        return {"matches": matches, **({"_skill_access_refs": refs} if refs else {})}

    def read(
        self,
        context,
        turn_id,
        *,
        conversation_id=None,
        offset=0,
        max_chars=4000,
        artifact_offset=0,
        skill_authorizer=None,
    ):
        """按 Turn 回读一页原问答及当前工件目录。"""
        identity = self._conversation(context, conversation_id)
        if offset < 0 or artifact_offset < 0 or not 1 <= max_chars <= 8000:
            raise ValueError("invalid history page")
        messages = self.conversations.messages_for_turn(identity, turn_id)
        if not messages:
            raise LookupError("Turn was not found")
        refs = require_access(
            merge_access(*(m.skill_access_refs for m in messages)), skill_authorizer
        )
        text = "\n\n".join(f"{item.role.value}: {item.content}" for item in messages)
        artifacts = self.artifacts.repository.list_turn(
            identity,
            turn_id,
            context.tenant_id,
            context.subject_id,
            limit=20,
            offset=artifact_offset,
        )
        return {
            **({"_skill_access_refs": refs} if refs else {}),
            "turn_id": turn_id,
            "historical": True,
            "content": text[offset : offset + max_chars],
            "next_offset": offset + max_chars if offset + max_chars < len(text) else None,
            "sources": [
                {"message_id": item.message_id, "content_hash": item.content_hash}
                for item in messages
            ],
            "next_artifact_offset": artifact_offset + 20 if len(artifacts) == 20 else None,
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "content_hash": item.content_hash,
                    "size_bytes": item.size_bytes,
                    "status": self._artifact_status(item),
                    "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                }
                for item in artifacts
            ],
        }

    def _artifact_status(self, metadata) -> str:
        """目录保留过期和已删除条目，避免暗示历史快照仍可读取。"""
        if metadata.deleted_at:
            return "deleted"
        expires = metadata.expires_at
        if expires and expires.replace(tzinfo=expires.tzinfo or UTC) <= datetime.now(UTC):
            return "protected" if self.artifacts.repository.is_protected(metadata) else "expired"
        return "available"

    def read_artifact(
        self,
        context,
        artifact_id,
        content_hash,
        *,
        mode="inspect",
        path="",
        fields=(),
        start=0,
        limit=20,
        offset=0,
        max_chars=4000,
        skill_authorizer=None,
    ):
        """回读已经归档的字节快照；外部链接不会被自动再次抓取。"""
        if offset < 0 or not 1 <= max_chars <= 8000:
            raise ValueError("invalid artifact page")
        metadata = self.artifacts.repository.get_owned(
            artifact_id, context.tenant_id, context.subject_id
        )
        if content_hash != metadata.content_hash:
            raise ValueError("artifact reference hash mismatch")
        raw = self.artifacts.read(artifact_id, context=context, skill_authorizer=skill_authorizer)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact is binary; text projection is unavailable") from exc
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            payload = text
        kind, data = business_view(payload)
        base = {
            "artifact_id": artifact_id,
            "content_hash": content_hash,
            "historical": True,
            "source_turn_id": metadata.source_turn_id,
            "size_bytes": metadata.size_bytes,
        }
        return read_view(
            kind,
            data,
            base,
            mode=mode,
            path=path,
            fields=fields,
            start=start,
            limit=limit,
            offset=offset,
            max_chars=max_chars,
        )
