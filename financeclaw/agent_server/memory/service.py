"""原生 Store 上的画像字段与事件；业务层只负责证据、确认和生命周期。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from langgraph.store.base import BaseStore, GetOp, Item

from financeclaw.agent_server.memory.models import (
    MemoryDraft,
    MemoryProposal,
    MemoryProvenance,
    MemoryRecall,
    MemoryRecord,
    MemoryStatus,
)
from financeclaw.agent_server.memory.policy import MemoryPolicy
from financeclaw.agent_server.memory.profiles import ProfileField
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.audit.repository import AuditRepository
from financeclaw.shared.conversation.models import MessageRole
from financeclaw.shared.conversation.repository import ConversationRepository
from financeclaw.shared.memory.deletion import deletion_event
from financeclaw.shared.memory.namespace import owner_namespace
from financeclaw.shared.memory.observability import store_operation
from financeclaw.shared.outbox.repository import OutboxRepository


class MemoryServiceError(RuntimeError):
    """可以向工具调用方解释的记忆操作错误。"""

    def __init__(self, message: str, *, reason: str) -> None:
        """保存可公开的领域错误原因。"""
        super().__init__(message)
        self.reason = reason


class MemoryEvidenceError(MemoryServiceError):
    """证据不存在、来源不可信或不属于当前主体。"""


class MemoryConfirmationRequired(MemoryServiceError):
    """有效记忆写入尚未经过必要确认。"""


class MemoryNotFound(MemoryServiceError):
    """当前主体下没有该记录。"""


class MemoryConflict(MemoryServiceError):
    """来源、内容或存储记录与本次请求冲突。"""


class MemoryStoreUnavailable(MemoryServiceError):
    """执行环境未提供原生 Store。"""


class MemoryReceiptPending(MemoryServiceError):
    """Store 已变更，但审计回执尚未完成；不可把该错误描述为已回滚。"""


def _hash(value: Any) -> str:
    """对规范化数据生成稳定摘要，不在回执中保留正文。"""
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()


class LongTermMemoryService:
    """字段直接 get，事件原生 search；不建设第二个向量存储或画像版本表。"""

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        audit: AuditRepository,
        policy: MemoryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        outbox: OutboxRepository | None = None,
    ) -> None:
        """保存服务依赖和用于有效期判定的时钟。"""
        self.conversations = conversation_repository
        self.audit = audit
        self.outbox = outbox
        self.policy = policy or MemoryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def namespace(context: ExecutionContext, category: str = "events") -> tuple[str, ...]:
        """画像与事件使用同一原生 Store 的独立 namespace。"""
        if category not in {"profile", "events"}:
            raise ValueError("unknown memory category")
        return (*owner_namespace(context), category)

    @staticmethod
    def _require_store(store: BaseStore | None) -> BaseStore:
        """缺少框架 Store 时明确停止记忆操作。"""
        if store is None:
            raise MemoryStoreUnavailable(
                "LangGraph Store is unavailable", reason="store_unavailable"
            )
        return store

    @staticmethod
    def _require_scope(context: ExecutionContext, scope: str) -> None:
        """只使用可信执行上下文中的权限上界。"""
        if "*" not in context.scopes and scope not in context.scopes:
            raise PermissionError(f"{scope} scope is required")

    def _evidence(
        self, context: ExecutionContext, draft: MemoryDraft
    ) -> tuple[MemoryDraft, tuple[str, ...]]:
        """读取同主体、同会话的用户原文并规范化证据 ID。"""
        if context.conversation_id is None:
            raise MemoryEvidenceError("conversation is required", reason="conversation_required")
        self.conversations.get_owned(context.conversation_id, context.tenant_id, context.subject_id)
        current = None
        if "current" in draft.evidence_message_ids:
            current = next(
                (
                    item
                    for item in self.conversations.messages_for_turn(
                        context.conversation_id, context.turn_id
                    )
                    if item.role is MessageRole.USER
                ),
                None,
            )
        messages = []
        for identity in draft.evidence_message_ids:
            if identity == "current":
                message = current
            else:
                try:
                    message = self.conversations.get_message_owned(
                        identity, context.tenant_id, context.subject_id
                    )
                except LookupError as exc:
                    raise MemoryEvidenceError(
                        "evidence was not found for this owner", reason="evidence_not_found"
                    ) from exc
            if message is None or message.conversation_id != context.conversation_id:
                raise MemoryEvidenceError(
                    "evidence is outside this conversation", reason="evidence_not_found"
                )
            if message.role is not MessageRole.USER:
                raise MemoryEvidenceError(
                    "user-authored evidence required", reason="user_evidence_required"
                )
            if not message.visible:
                raise MemoryEvidenceError("evidence is hidden", reason="evidence_not_found")
            if message.message_id not in {item.message_id for item in messages}:
                messages.append(message)
        normalized = draft.model_copy(
            update={"evidence_message_ids": tuple(item.message_id for item in messages)}
        )
        return normalized, tuple(item.content for item in messages)

    def propose(self, context: ExecutionContext, draft: MemoryDraft) -> MemoryProposal:
        """内部计算规范化提案与确认需求，不暴露第二个提案工具。"""
        self._require_scope(context, "memory:write")
        normalized, texts = self._evidence(context, draft)
        if normalized.valid_until is not None and normalized.valid_until <= self._clock():
            raise MemoryConflict("memory validity has already ended", reason="memory_expired")
        sensitivity, confirmation, reason = self.policy.assess(normalized, evidence_texts=texts)
        identity = _hash([context.tenant_id, context.subject_id, normalized.model_dump()])
        return MemoryProposal(
            proposal_id=f"proposal-{identity}",
            draft=normalized,
            sensitivity=sensitivity,
            requires_confirmation=confirmation,
            confirmation_reason=reason,
            policy_version=self.policy.version,
        )

    def requires_approval(self, context: ExecutionContext, arguments: dict[str, Any]) -> bool:
        """供原生 HITL 的 when 回调使用；模型不能自行声明免确认。"""
        draft = MemoryDraft.model_validate(
            {key: value for key, value in arguments.items() if key in MemoryDraft.model_fields}
        )
        return self.propose(context, draft).requires_confirmation

    def save(
        self,
        context: ExecutionContext,
        store: BaseStore,
        *,
        draft: MemoryDraft,
        mutation_id: str,
        approved: bool = False,
        supersedes_id: str | None = None,
    ) -> MemoryRecord:
        """更新单个画像字段或保存事件；重复执行同一工具调用复用结果并补齐审计。"""
        target = self._require_store(store)
        proposal = self.propose(context, draft)
        if proposal.requires_confirmation and not approved:
            raise MemoryConfirmationRequired(
                "native approval is required", reason="confirmation_required"
            )
        normalized = proposal.draft
        category = "profile" if normalized.field is not None else "events"
        namespace = self.namespace(context, category)
        key = (
            normalized.field.value
            if normalized.field
            else f"mem-{_hash([context.turn_id, mutation_id])}"
        )
        identity = f"profile:{key}" if normalized.field else key
        mutation = _hash([context.turn_id, mutation_id, normalized.model_dump()])
        existing_item = target.get(namespace, key)
        existing = self._project(existing_item, context) if existing_item else None
        if existing and existing.mutation_id == mutation:
            if existing.supersedes_id != supersedes_id:
                raise MemoryConflict("replacement changed", reason="mutation_conflict")
            self._finish_supersede(context, target, existing)
            self._audit(context, existing, "commit")
            return existing
        if existing and not normalized.field:
            raise MemoryConflict(
                "tool call identifies different memory facts", reason="mutation_conflict"
            )
        now = self._clock()
        record = MemoryRecord(
            memory_id=identity,
            namespace=namespace,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            memory_type=normalized.kind,
            field=normalized.field,
            mutation_id=mutation,
            revision=existing.revision + 1 if existing else 1,
            content=normalized.content,
            source_message_ids=normalized.evidence_message_ids,
            sensitivity=proposal.sensitivity,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            supersedes_id=supersedes_id,
            valid_until=normalized.valid_until,
            provenance=MemoryProvenance(
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
            ),
        )
        if supersedes_id:
            previous = self.get(context, target, supersedes_id)
            if (
                previous is None
                or previous.field
                or normalized.field
                or previous.memory_id == identity
            ):
                raise MemoryConflict("invalid event replacement", reason="supersede_target_invalid")
        target.put(
            namespace,
            key,
            record.model_dump(mode="json"),
            index=False if normalized.field else ["content"],
        )
        self._finish_supersede(context, target, record)
        self._audit(context, record, "commit")
        return record

    def _finish_supersede(
        self, context: ExecutionContext, store: BaseStore, record: MemoryRecord
    ) -> None:
        """重入时补齐旧事件失效；已经删除的旧事件不被重新创建。"""
        if not record.supersedes_id:
            return
        previous = self.get(context, store, record.supersedes_id, include_inactive=True)
        if previous is None:
            return
        if previous.status is MemoryStatus.SUPERSEDED:
            if previous.updated_at == record.updated_at:
                self._audit(context, previous, "supersede")
            return
        if previous.status is not MemoryStatus.ACTIVE:
            return
        old = previous.model_copy(
            update={"status": MemoryStatus.SUPERSEDED, "updated_at": record.updated_at}
        )
        store.put(old.namespace, old.memory_id, old.model_dump(mode="json"), index=False)
        self._audit(context, old, "supersede")

    def _project(self, item: Item, context: ExecutionContext) -> MemoryRecord:
        """校验 Store 记录及 namespace 的主体归属。"""
        record = MemoryRecord.model_validate(item.value)
        if (
            record.namespace != item.namespace
            or record.namespace[:-1] != owner_namespace(context)
            or (record.tenant_id, record.subject_id) != (context.tenant_id, context.subject_id)
        ):
            raise MemoryConflict(
                "stored identity does not match owner", reason="stored_scope_mismatch"
            )
        expected_key = record.field.value if record.field else record.memory_id
        expected_id = f"profile:{record.field.value}" if record.field else record.memory_id
        if item.key != expected_key or record.memory_id != expected_id:
            raise MemoryConflict("stored identity does not match key", reason="stored_key_mismatch")
        return record

    def _active(self, record: MemoryRecord) -> bool:
        """同时检查记录状态与业务有效期。"""
        return record.status is MemoryStatus.ACTIVE and (
            record.valid_until is None or record.valid_until > self._clock()
        )

    def profile(self, context: ExecutionContext, store: BaseStore) -> tuple[MemoryRecord, ...]:
        """按注册字段批量读取全部有效画像，完全不依赖 query 和 top-k。"""
        self._require_scope(context, "memory:read")
        target = self._require_store(store)
        items = target.batch(
            [GetOp(self.namespace(context, "profile"), field.value) for field in ProfileField]
        )
        records = [self._project(item, context) for item in items if item is not None]
        return tuple(record for record in records if self._active(record))

    def search(
        self, context, store, *, query=None, kinds=None, limit=6, purpose="explicit_memory_search"
    ) -> tuple[MemoryRecall, ...]:
        """只搜索事件；有界过取后复验有效期，不混算词法和向量分数。"""
        self._require_scope(context, "memory:read")
        if not 1 <= limit <= 20:
            raise ValueError("memory search limit must be between 1 and 20")
        target = self._require_store(store)
        namespace = self.namespace(context)
        filters = {"status": "active"}
        # The cheap existence probe avoids query embedding on an empty namespace.
        if not target.search(namespace, filter=filters, limit=1):
            return ()
        with store_operation(purpose, query_chars=len(query or "")):
            items = target.search(
                namespace, query=query or None, filter=filters, limit=min(60, limit * 3)
            )
        selected = []
        for item in items:
            record = self._project(item, context)
            if not self._active(record) or (kinds and record.memory_type not in kinds):
                continue
            selected.append(
                MemoryRecall(
                    record=record,
                    reason="semantic_event" if query else "event_listing",
                    score=float(item.score or 0),
                )
            )
            if len(selected) == limit:
                break
        return tuple(selected)

    def get(self, context, store, memory_id, *, include_inactive=False):
        """按 ID 读取；画像 ID 明确包含字段类别。"""
        self._require_scope(context, "memory:read")
        is_profile = memory_id.startswith("profile:")
        key = memory_id.removeprefix("profile:") if is_profile else memory_id
        item = self._require_store(store).get(
            self.namespace(context, "profile" if is_profile else "events"), key
        )
        if item is None:
            return None
        record = self._project(item, context)
        return record if include_inactive or self._active(record) else None

    def forget(self, context, store, memory_id, *, mode):
        """撤销退出召回；删除使用原生 Store.delete 移除正文与索引。"""
        self._require_scope(context, "memory:delete")
        if mode not in {"revoke", "delete"}:
            raise ValueError("invalid memory deletion mode")
        target = self._require_store(store)
        # Deletion authority does not implicitly grant arbitrary memory reading to the model.
        is_profile = memory_id.startswith("profile:")
        key = memory_id.removeprefix("profile:") if is_profile else memory_id
        namespace = self.namespace(context, "profile" if is_profile else "events")
        item = target.get(namespace, key)
        if item is None:
            return {"memory_id": memory_id, "status": "absent"}
        record = self._project(item, context)
        if mode == "delete":
            result = record.model_copy(update={"status": MemoryStatus.DELETED})
            audit = self._audit_record(context, result, mode)
            if self.outbox is not None:
                self.outbox.enqueue(deletion_event(record, key, audit))
            target.delete(namespace, key)
        else:
            result = record
            if record.status is not MemoryStatus.REVOKED:
                result = record.model_copy(
                    update={"status": MemoryStatus.REVOKED, "updated_at": self._clock()}
                )
                target.put(namespace, key, result.model_dump(mode="json"), index=False)
        self._audit(context, result, mode)
        return {"memory_id": memory_id, "status": result.status.value}

    def _audit(self, context, record, action):
        """幂等写入回执；部分成功时明确告诉调用方 Store 没有回滚。"""
        try:
            self.audit.append(self._audit_record(context, record, action))
        except Exception as exc:
            raise MemoryReceiptPending(
                "Store was changed; its audit receipt is pending. Retry the same operation.",
                reason="memory_receipt_pending",
            ) from exc

    def _audit_record(self, context, record, action):
        """生成不含记忆正文的稳定审计记录。"""
        events = {
            "commit": AuditEventType.MEMORY_COMMITTED,
            "supersede": AuditEventType.MEMORY_SUPERSEDED,
            "revoke": AuditEventType.MEMORY_REVOKED,
            "delete": AuditEventType.MEMORY_DELETED,
        }
        return AuditRecord(
            audit_id=f"audit-memory-{_hash([record.memory_id, record.mutation_id, action])}",
            occurred_at=record.updated_at,
            event_type=events[action],
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            resource_type="memory",
            resource_id=record.memory_id,
            resource_version=str(record.revision),
            action=action,
            decision=record.status.value,
            policy_version=self.policy.version,
            payload_hash=_hash(record.model_dump()),
            evidence_refs=record.source_message_ids,
            metadata={
                "sensitivity": record.sensitivity.value,
                "mutation_id": record.mutation_id,
            },
        )
