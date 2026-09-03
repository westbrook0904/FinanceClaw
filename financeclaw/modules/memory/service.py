"""管理长期记忆的提议、确认、检索、撤销与删除生命周期。"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from langgraph.store.base import BaseStore, PutOp
from langsmith import traceable

from financeclaw.kernel import ExecutionContext
from financeclaw.modules.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.modules.conversation import ConversationRepository, MessageRole

from .models import (
    MemoryDraft,
    MemoryProposal,
    MemoryProvenance,
    MemoryRecall,
    MemoryRecord,
    MemorySensitivity,
    MemoryStatus,
    MemoryType,
)
from .policy import MemoryPolicy

_TERM = re.compile(r"[A-Za-z0-9._-]{2,}|[\u4e00-\u9fff]{2,}")
_STORE_ROOT = ("financeclaw", "long-term-memory", "v1")


class MemoryServiceError(RuntimeError):
    """定义记忆ServiceError。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。

    属性：
        reason: 产生当前决策、遗漏或状态的可读原因。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        """注入并保存记忆ServiceError所需的协作对象，同时校验构造期不变量。"""
        super().__init__(message)
        self.reason = reason


class MemoryEvidenceError(MemoryServiceError):
    """定义记忆EvidenceError。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class MemoryConfirmationRequired(MemoryServiceError):
    """定义记忆ConfirmationRequired。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。
    """

    pass


class MemoryNotFound(MemoryServiceError):
    """定义记忆NotFound。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class MemoryConflict(MemoryServiceError):
    """定义记忆Conflict。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class MemoryStoreUnavailable(MemoryServiceError):
    """定义记忆StoreUnavailable。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


@traceable(name="memory.recall", run_type="retriever", tags=["stage:3"])
def trace_memory_recall(
    *, query_hash: str, memory_ids: tuple[str, ...], context_metadata: dict[str, str]
) -> None:
    """记录长期记忆检索数量与命中标识，不记录原始敏感内容。"""
    del query_hash, memory_ids, context_metadata


@traceable(name="memory.write", run_type="chain", tags=["stage:3"])
def trace_memory_write(*, action: str, memory_id: str, context_metadata: dict[str, str]) -> None:
    """记录长期记忆写入事件及稳定标识。"""
    del action, memory_id, context_metadata


class LongTermMemoryService:
    """实施长期记忆的证据校验、确认策略、版本更新与审计。

    适用场景：
        用于应用用例需要跨仓储、外部端口或领域策略协调一致结果的场景。

    属性：
        conversations: 用于验证记忆证据消息归属的会话仓储。
        audit: 记录授权、执行和状态变化的审计仓储。
        policy: 在副作用执行前作出确定性授权或记忆处理决定的策略。
        _clock: 可替换时间源，便于统一 UTC 时间并支持确定性测试。
    """

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        audit: AuditRepository,
        policy: MemoryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """注入并保存长期Term记忆Service所需的协作对象，同时校验构造期不变量。"""
        self.conversations = conversation_repository
        self.audit = audit
        self.policy = policy or MemoryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def namespace(context: ExecutionContext) -> tuple[str, ...]:
        """根据租户、主体和记忆类型构造隔离的 Store 命名空间。"""
        return (
            *_STORE_ROOT,
            _namespace_label(context.tenant_id),
            _namespace_label(context.subject_id),
        )

    def propose(self, context: ExecutionContext, draft: MemoryDraft) -> MemoryProposal:
        """验证消息证据和记忆策略，创建待确认提议或直接提交低风险记忆。"""
        resolved = draft.model_copy(
            update={"evidence_message_ids": self._resolve_evidence(context, draft)}
        )
        sensitivity, confirmation, reason = self.policy.assess(resolved)
        proposal = MemoryProposal(
            proposal_id=self._proposal_id(context, resolved),
            draft=resolved,
            sensitivity=sensitivity,
            requires_confirmation=confirmation,
            confirmation_reason=reason,
            policy_version=self.policy.version,
        )
        self._audit(
            context,
            event=AuditEventType.MEMORY_PROPOSED,
            resource_id=proposal.proposal_id,
            action="propose",
            decision="validated",
            payload=self._proposal_facts(proposal),
            evidence=proposal.draft.evidence_message_ids,
            sensitivity=sensitivity,
        )
        return proposal

    def confirm(
        self,
        context: ExecutionContext,
        store: BaseStore | None,
        *,
        proposal_id: str,
        draft: MemoryDraft,
        user_confirmed: bool,
        supersedes_id: str | None = None,
    ) -> MemoryRecord:
        """校验提议归属及确认权限，写入新记忆并替代指定旧版本。"""
        target_store = self._require_store(store)
        normalized = draft.model_copy(
            update={"evidence_message_ids": self._resolve_evidence(context, draft)}
        )
        sensitivity, confirmation, _ = self.policy.assess(normalized)
        expected_proposal_id = self._proposal_id(context, normalized)
        if proposal_id != expected_proposal_id:
            raise MemoryConflict(
                "proposal ID does not match the confirmed memory facts",
                reason="proposal_mismatch",
            )
        if confirmation and not user_confirmed:
            raise MemoryConfirmationRequired(
                "explicit user confirmation is required before memory persistence",
                reason="confirmation_required",
            )

        namespace = self.namespace(context)
        memory_id = self._memory_id(context, proposal_id)
        existing = self._read(target_store, namespace, memory_id)
        if existing is not None:
            self._verify_scope(existing, context)
            if existing.status is MemoryStatus.ACTIVE and self._same_facts(
                existing, normalized, supersedes_id
            ):
                return existing
            raise MemoryConflict(
                "proposal ID already identifies different or inactive memory facts",
                reason="memory_identity_conflict",
            )

        now = self._now()
        replaced: MemoryRecord | None = None
        if supersedes_id is not None:
            replaced = self._read(target_store, namespace, supersedes_id)
            if replaced is not None:
                self._verify_scope(replaced, context)
            if replaced is None or replaced.status is not MemoryStatus.ACTIVE:
                raise MemoryConflict(
                    "only an active memory in the trusted namespace can be superseded",
                    reason="supersede_target_invalid",
                )
            replaced = replaced.model_copy(
                update={"status": MemoryStatus.SUPERSEDED, "updated_at": now}
            )

        record = MemoryRecord(
            memory_id=memory_id,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            namespace=namespace,
            memory_type=normalized.kind,
            content=normalized.content,
            status=MemoryStatus.ACTIVE,
            source_message_ids=normalized.evidence_message_ids,
            created_at=now,
            updated_at=now,
            supersedes_id=supersedes_id,
            sensitivity=sensitivity,
            provenance=MemoryProvenance(
                conversation_id=self._conversation_id(context),
                turn_id=context.turn_id,
                run_id=context.run_id,
            ),
        )
        operations = [self._put_operation(record)]
        if replaced is not None:
            operations.append(self._put_operation(replaced))
        target_store.batch(operations)

        self._audit(
            context,
            event=AuditEventType.MEMORY_COMMITTED,
            resource_id=record.memory_id,
            action="commit",
            decision="committed",
            payload=record.model_dump(mode="json"),
            evidence=record.source_message_ids,
            sensitivity=record.sensitivity,
        )
        if replaced is not None:
            self._audit(
                context,
                event=AuditEventType.MEMORY_SUPERSEDED,
                resource_id=replaced.memory_id,
                action="supersede",
                decision=f"superseded_by:{record.memory_id}",
                payload=replaced.model_dump(mode="json"),
                evidence=replaced.source_message_ids,
                sensitivity=replaced.sensitivity,
            )
        trace_memory_write(
            action="commit",
            memory_id=record.memory_id,
            context_metadata=context.trace_metadata(),
        )
        return record

    def search(
        self,
        context: ExecutionContext,
        store: BaseStore | None,
        *,
        query: str | None = None,
        kinds: Sequence[MemoryType] | None = None,
        limit: int = 10,
        for_model_context: bool = False,
    ) -> tuple[MemoryRecall, ...]:
        """按查询条件检索长期Term记忆Service并返回排序结果。"""
        if limit < 1 or limit > 50:
            raise ValueError("memory search limit must be between 1 and 50")
        target_store = self._require_store(store)
        namespace = self.namespace(context)
        raw = target_store.search(
            namespace,
            query=query or None,
            filter={"status": MemoryStatus.ACTIVE.value},
            limit=50,
        )
        allowed_kinds = set(kinds or MemoryType)
        now = self._now()
        recalls: list[MemoryRecall] = []
        for item in raw:
            record = self._project(item.value, expected_namespace=namespace)
            self._verify_scope(record, context)
            if record.status is not MemoryStatus.ACTIVE or record.memory_type not in allowed_kinds:
                continue
            if record.valid_until is not None and record.valid_until <= now:
                continue
            lexical = _relevance(query or "", record.content)
            semantic = float(item.score or 0)
            stable_context = record.memory_type in {MemoryType.CONSTRAINT, MemoryType.GOAL}
            if (
                for_model_context
                and not stable_context
                and query
                and lexical == 0
                and semantic == 0
            ):
                continue
            reason = (
                "active_constraint"
                if record.memory_type is MemoryType.CONSTRAINT
                else "active_goal"
                if record.memory_type is MemoryType.GOAL
                else "semantic_relevance"
                if semantic > 0
                else "lexical_relevance"
                if lexical > 0
                else "explicit_memory_search"
            )
            recalls.append(
                MemoryRecall(record=record, reason=reason, score=max(semantic, float(lexical)))
            )
        recalls.sort(
            key=lambda item: (
                item.score,
                item.record.updated_at.timestamp(),
                item.record.memory_id,
            ),
            reverse=True,
        )
        selected = tuple(recalls[:limit])
        trace_memory_recall(
            query_hash=_hash({"query": query or ""}),
            memory_ids=tuple(item.record.memory_id for item in selected),
            context_metadata=context.trace_metadata(),
        )
        return selected

    def get(
        self,
        context: ExecutionContext,
        store: BaseStore | None,
        memory_id: str,
        *,
        include_inactive: bool = False,
    ) -> MemoryRecord | None:
        """按标识读取长期Term记忆Service；不存在时由下层仓储抛出明确异常。"""
        record = self._read(self._require_store(store), self.namespace(context), memory_id)
        if record is None:
            return None
        self._verify_scope(record, context)
        if not include_inactive and record.status is not MemoryStatus.ACTIVE:
            return None
        return record

    def forget(
        self,
        context: ExecutionContext,
        store: BaseStore | None,
        memory_id: str,
        *,
        mode: Literal["revoke", "delete"],
    ) -> MemoryRecord:
        """校验归属后撤销或永久删除记忆，并留下对应审计事件。"""
        target_store = self._require_store(store)
        namespace = self.namespace(context)
        record = self._read(target_store, namespace, memory_id)
        if record is None:
            raise MemoryNotFound("memory was not found", reason="memory_not_found")
        self._verify_scope(record, context)
        target_status = MemoryStatus.REVOKED if mode == "revoke" else MemoryStatus.DELETED
        if record.status is target_status:
            return record
        if mode == "revoke" and record.status is not MemoryStatus.ACTIVE:
            raise MemoryConflict(
                "only active memory can be revoked",
                reason="invalid_lifecycle_transition",
            )
        updated = record.model_copy(update={"status": target_status, "updated_at": self._now()})
        target_store.put(namespace, updated.memory_id, updated.model_dump(mode="json"), index=False)
        event = (
            AuditEventType.MEMORY_REVOKED
            if target_status is MemoryStatus.REVOKED
            else AuditEventType.MEMORY_DELETED
        )
        self._audit(
            context,
            event=event,
            resource_id=updated.memory_id,
            action=mode,
            decision=target_status.value,
            payload=updated.model_dump(mode="json"),
            evidence=updated.source_message_ids,
            sensitivity=updated.sensitivity,
        )
        trace_memory_write(
            action=mode,
            memory_id=updated.memory_id,
            context_metadata=context.trace_metadata(),
        )
        return updated

    def _resolve_evidence(self, context: ExecutionContext, draft: MemoryDraft) -> tuple[str, ...]:
        """解析并校验长期Term记忆Service，返回固定版本的运行对象。"""
        conversation_id = self._conversation_id(context)
        self.conversations.get_owned(conversation_id, context.tenant_id, context.subject_id)
        messages = self.conversations.list_messages(conversation_id)
        current = [
            message
            for message in messages
            if message.turn_id == context.turn_id and message.role is MessageRole.USER
        ]
        current_id = current[-1].message_id if current else None
        requested = tuple(
            current_id if item == "current" and current_id is not None else item
            for item in draft.evidence_message_ids
        )
        known = {message.message_id: message for message in messages}
        if any(item not in known for item in requested):
            raise MemoryEvidenceError(
                "memory evidence must resolve to the owned conversation Journal",
                reason="evidence_not_found",
            )
        if not any(known[item].role is MessageRole.USER for item in requested):
            raise MemoryEvidenceError(
                "memory evidence must include at least one user-authored message",
                reason="user_evidence_required",
            )
        return requested

    @staticmethod
    def _require_store(store: BaseStore | None) -> BaseStore:
        """确认当前调用已配置长期记忆 Store，否则返回明确能力错误。"""
        if store is None:
            raise MemoryStoreUnavailable(
                "LangGraph Store is unavailable for this execution",
                reason="store_unavailable",
            )
        return store

    @staticmethod
    def _read(store: BaseStore, namespace: tuple[str, ...], memory_id: str) -> MemoryRecord | None:
        """从 Store 读取条目并转换为长期记忆记录。"""
        item = store.get(namespace, memory_id)
        return (
            None
            if item is None
            else LongTermMemoryService._project(item.value, expected_namespace=namespace)
        )

    @staticmethod
    def _project(value: Mapping[str, Any], *, expected_namespace: tuple[str, ...]) -> MemoryRecord:
        """把长期记忆记录转换为不泄露内部 Store 结构的公开字典。"""
        try:
            record = MemoryRecord.model_validate(value)
        except Exception as exc:
            raise MemoryConflict(
                "LangGraph Store returned an invalid memory record",
                reason="invalid_store_record",
            ) from exc
        if record.namespace != expected_namespace:
            raise MemoryConflict(
                "stored memory namespace does not match its trusted Store path",
                reason="namespace_mismatch",
            )
        return record

    @staticmethod
    def _verify_scope(record: MemoryRecord, context: ExecutionContext) -> None:
        """校验记忆记录属于当前租户与主体。"""
        if record.tenant_id != context.tenant_id or record.subject_id != context.subject_id:
            raise MemoryConflict(
                "stored memory identity does not match trusted execution context",
                reason="stored_scope_mismatch",
            )

    @staticmethod
    def _put_operation(record: MemoryRecord) -> PutOp:
        """构造可幂等重放的 Store 写操作描述。"""
        index: list[str] | Literal[False] = (
            ["content"] if record.status is MemoryStatus.ACTIVE else False
        )
        return PutOp(
            record.namespace,
            record.memory_id,
            record.model_dump(mode="json"),
            index=index,
        )

    @staticmethod
    def _same_facts(record: MemoryRecord, draft: MemoryDraft, supersedes_id: str | None) -> bool:
        """比较候选与既有记录的事实字段，判断是否为安全重放。"""
        return (
            record.memory_type is draft.kind
            and record.content == draft.content
            and record.source_message_ids == draft.evidence_message_ids
            and record.supersedes_id == supersedes_id
        )

    def _proposal_id(self, context: ExecutionContext, draft: MemoryDraft) -> str:
        """由作用域、事实和证据生成确定性记忆提议标识。"""
        payload = {
            "tenant_id": context.tenant_id,
            "subject_id": context.subject_id,
            "draft": draft.model_dump(mode="json"),
            "policy_version": self.policy.version,
        }
        return f"proposal-{_hash(payload)}"

    @staticmethod
    def _memory_id(context: ExecutionContext, proposal_id: str) -> str:
        """由作用域、事实与 schema 版本生成确定性长期记忆标识。"""
        identity = {
            "tenant_id": context.tenant_id,
            "subject_id": context.subject_id,
            "proposal_id": proposal_id,
        }
        return f"mem-{_hash(identity)}"

    @staticmethod
    def _proposal_facts(proposal: MemoryProposal) -> dict[str, Any]:
        """抽取决定提议身份的规范化事实字段。"""
        return proposal.model_dump(mode="json")

    @staticmethod
    def _conversation_id(context: ExecutionContext) -> str:
        """从证据消息解析并验证唯一会话归属。"""
        if context.conversation_id is None:
            raise MemoryEvidenceError(
                "conversation_id is required for memory evidence",
                reason="conversation_required",
            )
        return context.conversation_id

    def _now(self) -> datetime:
        """从可替换时间源取得带 UTC 时区的当前时间。"""
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise TypeError("memory clock must return a timezone-aware datetime")
        return now

    def _audit(
        self,
        context: ExecutionContext,
        *,
        event: AuditEventType,
        resource_id: str,
        action: str,
        decision: str,
        payload: Mapping[str, Any],
        evidence: tuple[str, ...],
        sensitivity: MemorySensitivity,
    ) -> None:
        """构造不可变审计事件并写入审计仓储。"""
        self.audit.append(
            AuditRecord(
                event_type=event,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                run_id=context.run_id,
                resource_type="memory",
                resource_id=resource_id,
                resource_version="1",
                action=action,
                decision=decision,
                policy_version=self.policy.version,
                payload_hash=_hash(payload),
                evidence_refs=evidence,
                metadata={"sensitivity": sensitivity.value},
            )
        )


def _namespace_label(value: str) -> str:
    """将 Store 命名空间转换为可读标签，供审计和诊断使用。"""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _hash(value: Mapping[str, Any]) -> str:
    """对规范化内容计算稳定 SHA-256，供幂等、审批绑定或审计使用。"""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(canonical.encode()).hexdigest()


def _relevance(query: str, content: str) -> int:
    """根据查询词项、记忆内容和状态计算轻量相关性得分。"""
    if not query:
        return 0
    if query.casefold() in content.casefold():
        return 1
    query_terms = _terms(query)
    content_terms = _terms(content)
    return len(query_terms.intersection(content_terms))


def _terms(value: str) -> set[str]:
    """规范化文本并提取用于轻量相关性计算的词项集合。"""
    terms: set[str] = set()
    for term in _TERM.findall(value):
        normalized = term.casefold()
        if any("\u4e00" <= character <= "\u9fff" for character in normalized):
            terms.update(normalized[index : index + 2] for index in range(len(normalized) - 1))
        else:
            terms.add(normalized)
    return terms
