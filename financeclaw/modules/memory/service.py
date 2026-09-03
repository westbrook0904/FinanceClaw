"""长期记忆的受控读写服务：证据解析、召回、提交确认与生命周期管理。

服务基于 LangGraph Store 按可信租户/主体命名空间隔离记忆，配合 HITL 确认、
LangSmith 观测 span 与永久审计，实现可治理的跨会话长期记忆。
"""

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

# 检索相关性分词规则：拉丁字母/数字串，或连续汉字切成的二元组。
_TERM = re.compile(r"[A-Za-z0-9._-]{2,}|[\u4e00-\u9fff]{2,}")
# LangGraph Store 命名空间根路径，后接租户与主体的转义标签，固定共 5 段。
_STORE_ROOT = ("financeclaw", "long-term-memory", "v1")


class MemoryServiceError(RuntimeError):
    """记忆模块所有服务层异常的基类。

    使用场景：
        调用方捕获本基类即可统一处理记忆读写失败，并通过 reason 细分原因。

    Attributes:
        reason: 机器可读的失败原因码，用于映射稳定错误响应。

    """

    def __init__(self, message: str, *, reason: str) -> None:
        """保存失败消息与机器可读原因码。"""
        super().__init__(message)
        self.reason = reason


class MemoryEvidenceError(MemoryServiceError):
    """记忆证据不满足要求时抛出的服务异常。

    使用场景：
        证据无法解析到当前主体拥有的会话、或缺少用户消息时抛出，
        调用方应拒绝写入并提示用户补充有效证据。
    """

    pass


class MemoryConfirmationRequired(MemoryServiceError):
    """策略要求显式确认而用户尚未确认时抛出的服务异常。

    使用场景：
        confirm 在需要确认但 user_confirmed 为假时抛出，
        调用方应把提案转交 HITL 流程等待用户决定。
    """

    pass


class MemoryNotFound(MemoryServiceError):
    """指定标识的记忆不存在时抛出的服务异常。

    使用场景：
        forget 读取不到目标记忆时抛出，调用方可视目标状态决定是否忽略。
    """

    pass


class MemoryConflict(MemoryServiceError):
    """记忆事实或身份与期望不一致时抛出的服务异常。

    使用场景：
        提案标识不匹配、命名空间错位、生命周期非法迁移等场景抛出，
        调用方应中止本次写入以保护既有记忆。
    """

    pass


class MemoryStoreUnavailable(MemoryServiceError):
    """当前执行上下文没有可用的 LangGraph Store 时抛出的服务异常。

    使用场景：
        Store 未注入执行图时抛出，调用方应降级为无长期记忆的会话。
    """

    pass


@traceable(name="memory.recall", run_type="retriever", tags=["stage:3"])
def trace_memory_recall(
    *, query_hash: str, memory_ids: tuple[str, ...], context_metadata: dict[str, str]
) -> None:
    """在 LangSmith 产生 memory.recall 检索 span，记录被注入上下文的记忆。

    仅承载观测元数据，参数随即丢弃，不执行任何业务逻辑。

    Args:
        query_hash: 检索查询的哈希，避免在 span 中泄露查询原文。
        memory_ids: 实际选中注入的记忆标识列表。
        context_metadata: 执行上下文的脱敏追踪元数据。

    """
    del query_hash, memory_ids, context_metadata


@traceable(name="memory.write", run_type="chain", tags=["stage:3"])
def trace_memory_write(*, action: str, memory_id: str, context_metadata: dict[str, str]) -> None:
    """在 LangSmith 产生 memory.write 链路 span，记录一次记忆写操作。

    仅承载观测元数据，参数随即丢弃，不执行任何业务逻辑。

    Args:
        action: 写操作类型，如 commit、revoke、delete。
        memory_id: 操作目标的记忆标识。
        context_metadata: 执行上下文的脱敏追踪元数据。

    """
    del action, memory_id, context_metadata


class LongTermMemoryService:
    """长期记忆的受控读写服务，协调证据、策略、Store 与审计。

    使用场景：
        由记忆中间件在会话流程中调用：propose 产出提案供 HITL 确认，
        confirm 在用户确认后落库，search/get 按命名空间召回记忆，
        forget 执行 revoke/delete 生命周期退出。

    Attributes:
        conversations: 会话仓储，用于校验证据消息归属与会话所有权。
        audit: 审计仓储，提案、提交与生命周期事件都会追加永久审计。
        policy: 记忆写入治理策略，propose 与 confirm 共用同一实例。

    """

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        audit: AuditRepository,
        policy: MemoryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """注入记忆服务的依赖。

        Args:
            conversation_repository: 会话仓储，用于解析与校验证据消息。
            audit: 审计仓储，用于记录提案与生命周期事件。
            policy: 记忆治理策略；缺省使用默认策略。
            clock: 返回带时区当前时间的可调用对象；缺省使用系统 UTC 时钟。

        """
        self.conversations = conversation_repository
        self.audit = audit
        self.policy = policy or MemoryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def namespace(context: ExecutionContext) -> tuple[str, ...]:
        """构造当前租户与主体的 Store 命名空间路径。

        由根路径 3 段与 URL 安全转义后的租户、主体标签组成，固定 5 段，
        保证不可信字符无法进入 Store 路径。
        """
        return (
            *_STORE_ROOT,
            _namespace_label(context.tenant_id),
            _namespace_label(context.subject_id),
        )

    def propose(self, context: ExecutionContext, draft: MemoryDraft) -> MemoryProposal:
        """校验证据并评估策略，产出待确认的记忆写入提案。

        Args:
            context: 当前执行上下文，提供会话、轮次与运行标识。
            draft: 待写入的记忆草案。

        Returns:
            携带确定性提案标识、敏感级别与确认要求的提案。

        """
        # 1. 解析证据：把 `current` 占位符解析为当前轮次用户消息并校验归属。
        resolved = draft.model_copy(
            update={"evidence_message_ids": self._resolve_evidence(context, draft)}
        )
        # 2. 策略评估：得到敏感级别与是否需要显式确认。
        sensitivity, confirmation, reason = self.policy.assess(resolved)
        # 3. 生成提案：proposal_id 由租户、主体、草案与策略版本共同决定。
        proposal = MemoryProposal(
            proposal_id=self._proposal_id(context, resolved),
            draft=resolved,
            sensitivity=sensitivity,
            requires_confirmation=confirmation,
            confirmation_reason=reason,
            policy_version=self.policy.version,
        )
        # 4. 追加 MEMORY_PROPOSED 审计事件后返回提案。
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
        """在用户确认后把提案落库为长期记忆，支持幂等与 supersede 取代。

        写入前复验提案一致性、策略结论与命名空间归属；若指定取代目标，
        同一事务中把旧记录置为 SUPERSEDED 并退出语义索引。

        Args:
            context: 当前执行上下文。
            store: LangGraph Store；不可用时抛出 MemoryStoreUnavailable。
            proposal_id: propose 阶段返回的提案标识。
            draft: 与提案一致的记忆草案。
            user_confirmed: 用户是否已显式确认本次写入。
            supersedes_id: 需要被取代的既有记忆标识；可为空。

        Returns:
            新写入或幂等命中的记忆记录。

        Raises:
            MemoryConflict: 提案不匹配、命名空间冲突或取代目标非法。
            MemoryConfirmationRequired: 策略要求确认但用户未确认。

        """
        target_store = self._require_store(store)
        # 1. 复评策略并重算提案标识，确保确认的事实与提案完全一致。
        normalized = draft.model_copy(
            update={"evidence_message_ids": self._resolve_evidence(context, draft)}
        )
        sensitivity, confirmation, _ = self.policy.assess(normalized)
        expected_proposal_id = self._proposal_id(context, normalized)
        # 2. 提案标识不匹配说明事实被篡改或提案过期，拒绝写入。
        if proposal_id != expected_proposal_id:
            raise MemoryConflict(
                "proposal ID does not match the confirmed memory facts",
                reason="proposal_mismatch",
            )
        # 3. 策略要求显式确认时，未经用户确认不得持久化。
        if confirmation and not user_confirmed:
            raise MemoryConfirmationRequired(
                "explicit user confirmation is required before memory persistence",
                reason="confirmation_required",
            )

        namespace = self.namespace(context)
        memory_id = self._memory_id(context, proposal_id)
        # 4. 幂等检查：同一提案重复确认且事实未变时直接返回既有记录。
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
        # 5. 处理取代：被取代记忆必须存在、归属正确且处于 ACTIVE 状态。
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
        # 6. 写入 Store：新记录参与语义索引，被取代记录退出索引。
        operations = [self._put_operation(record)]
        if replaced is not None:
            operations.append(self._put_operation(replaced))
        target_store.batch(operations)

        # 7. 追加 MEMORY_COMMITTED（及取代时的 MEMORY_SUPERSEDED）审计。
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
        """在当前命名空间检索可注入模型上下文的长期记忆。

        仅返回 ACTIVE 且未过期的记忆；constraint/goal 等稳定类别始终保留，
        其余类别在面向模型上下文且与查询零相关时被过滤，以节省独立 token 预算。

        Args:
            context: 当前执行上下文。
            store: LangGraph Store。
            query: 自然语言查询；为空时按词法退化为显式检索。
            kinds: 限定召回的记忆类别；为空表示全部类别。
            limit: 返回数量上限，取值 1 到 50。
            for_model_context: 是否面向模型上下文过滤（启用相关性与预算约束）。

        Returns:
            按得分、更新时间与标识排序的召回结果元组。

        Raises:
            ValueError: limit 超出允许范围。

        """
        # 1. 校验 limit 并在命名空间内检索 ACTIVE 状态的记忆。
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
        # 2. 逐条投影为记录并校验租户与主体归属。
        recalls: list[MemoryRecall] = []
        for item in raw:
            record = self._project(item.value, expected_namespace=namespace)
            self._verify_scope(record, context)
            # 3. 过滤非 ACTIVE、类别不符与已过期的记忆。
            if record.status is not MemoryStatus.ACTIVE or record.memory_type not in allowed_kinds:
                continue
            if record.valid_until is not None and record.valid_until <= now:
                continue
            # 4. 计算词法与语义相关性，并按预算约束过滤零相关的不稳定类别。
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
            # 5. 推导命中理由，得分取语义与词法的较大值。
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
        # 6. 按得分、更新时间与标识排序后截断到 limit，并上报检索 span。
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
        """按标识读取当前命名空间内的单条记忆。

        Args:
            context: 当前执行上下文。
            store: LangGraph Store。
            memory_id: 目标记忆标识。
            include_inactive: 是否允许返回非 ACTIVE 状态的记录。

        Returns:
            命中的记忆记录；不存在、归属不符或不允许返回时为 None。

        """
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
        """撤销（revoke）或删除（delete）指定记忆，实现生命周期退出。

        两类操作均幂等：目标已处于目标状态时直接返回既有记录；
        revoke 仅允许作用于 ACTIVE 记录，delete 可作用于任意状态。

        Args:
            context: 当前执行上下文。
            store: LangGraph Store。
            memory_id: 目标记忆标识。
            mode: 生命周期动作，revoke 表示撤销，delete 表示删除。

        Returns:
            更新为目标状态后的记忆记录。

        Raises:
            MemoryNotFound: 记忆不存在。
            MemoryConflict: revoke 的目标不是 ACTIVE 记录。

        """
        target_store = self._require_store(store)
        # 1. 读取并校验记忆归属。
        namespace = self.namespace(context)
        record = self._read(target_store, namespace, memory_id)
        if record is None:
            raise MemoryNotFound("memory was not found", reason="memory_not_found")
        self._verify_scope(record, context)
        target_status = MemoryStatus.REVOKED if mode == "revoke" else MemoryStatus.DELETED
        # 2. 幂等短路：目标已处于目标状态时直接返回。
        if record.status is target_status:
            return record
        # 3. 状态机校验：仅 ACTIVE 记录可被撤销。
        if mode == "revoke" and record.status is not MemoryStatus.ACTIVE:
            raise MemoryConflict(
                "only active memory can be revoked",
                reason="invalid_lifecycle_transition",
            )
        # 4. 写回目标状态并退出语义索引（index=False）。
        updated = record.model_copy(update={"status": target_status, "updated_at": self._now()})
        target_store.put(namespace, updated.memory_id, updated.model_dump(mode="json"), index=False)
        # 5. 追加 MEMORY_REVOKED 或 MEMORY_DELETED 审计并上报写 span。
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
        """解析并校验草案中的证据消息引用，返回最终证据标识元组。

        把 `current` 占位符替换为当前轮次最后一条用户消息，要求全部证据
        都属于当前主体拥有的会话，且至少包含一条用户消息。
        """
        # 1. 校验会话归属，防止引用他人会话作为证据。
        conversation_id = self._conversation_id(context)
        self.conversations.get_owned(conversation_id, context.tenant_id, context.subject_id)
        messages = self.conversations.list_messages(conversation_id)
        # 2. 解析 current 占位符为当前轮次的用户消息。
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
        # 3. 证据必须全部存在于当前主体拥有的会话 Journal 中。
        if any(item not in known for item in requested):
            raise MemoryEvidenceError(
                "memory evidence must resolve to the owned conversation Journal",
                reason="evidence_not_found",
            )
        # 4. 至少一条用户消息，保证记忆事实来自用户本人。
        if not any(known[item].role is MessageRole.USER for item in requested):
            raise MemoryEvidenceError(
                "memory evidence must include at least one user-authored message",
                reason="user_evidence_required",
            )
        return requested

    @staticmethod
    def _require_store(store: BaseStore | None) -> BaseStore:
        """确保执行上下文提供了 LangGraph Store，否则抛出 MemoryStoreUnavailable。"""
        if store is None:
            raise MemoryStoreUnavailable(
                "LangGraph Store is unavailable for this execution",
                reason="store_unavailable",
            )
        return store

    @staticmethod
    def _read(store: BaseStore, namespace: tuple[str, ...], memory_id: str) -> MemoryRecord | None:
        """从 Store 读取并投影单条记忆；不存在时返回 None。"""
        item = store.get(namespace, memory_id)
        return (
            None
            if item is None
            else LongTermMemoryService._project(item.value, expected_namespace=namespace)
        )

    @staticmethod
    def _project(value: Mapping[str, Any], *, expected_namespace: tuple[str, ...]) -> MemoryRecord:
        """把 Store 原始值投影为记录，并校验其命名空间与期望路径一致。"""
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
        """校验记录的租户与主体与当前执行上下文一致，防止跨主体读取。"""
        if record.tenant_id != context.tenant_id or record.subject_id != context.subject_id:
            raise MemoryConflict(
                "stored memory identity does not match trusted execution context",
                reason="stored_scope_mismatch",
            )

    @staticmethod
    def _put_operation(record: MemoryRecord) -> PutOp:
        """构造 Store 写入操作；仅 ACTIVE 记录对 content 建立语义索引。"""
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
        """判断既有记录与草案是否完全同事实，用于重复确认时的幂等短路。"""
        return (
            record.memory_type is draft.kind
            and record.content == draft.content
            and record.source_message_ids == draft.evidence_message_ids
            and record.supersedes_id == supersedes_id
        )

    def _proposal_id(self, context: ExecutionContext, draft: MemoryDraft) -> str:
        """计算由租户、主体、草案与策略版本共同决定的确定性提案标识。"""
        payload = {
            "tenant_id": context.tenant_id,
            "subject_id": context.subject_id,
            "draft": draft.model_dump(mode="json"),
            "policy_version": self.policy.version,
        }
        return f"proposal-{_hash(payload)}"

    @staticmethod
    def _memory_id(context: ExecutionContext, proposal_id: str) -> str:
        """由租户、主体与提案标识派生确定性的记忆标识。"""
        identity = {
            "tenant_id": context.tenant_id,
            "subject_id": context.subject_id,
            "proposal_id": proposal_id,
        }
        return f"mem-{_hash(identity)}"

    @staticmethod
    def _proposal_facts(proposal: MemoryProposal) -> dict[str, Any]:
        """返回提案的可审计事实快照（JSON 兼容字典）。"""
        return proposal.model_dump(mode="json")

    @staticmethod
    def _conversation_id(context: ExecutionContext) -> str:
        """读取上下文中的会话标识，缺失时抛出 MemoryEvidenceError。"""
        if context.conversation_id is None:
            raise MemoryEvidenceError(
                "conversation_id is required for memory evidence",
                reason="conversation_required",
            )
        return context.conversation_id

    def _now(self) -> datetime:
        """返回当前时间；时钟未返回带时区时间时直接失败，保证时间可比。"""
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
        """追加一条记忆资源的永久审计事件，携带载荷哈希、证据与敏感级别。"""
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
    """把标识转义为去掉填充的 URL 安全 Base64 标签，用于 Store 命名空间。"""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _hash(value: Mapping[str, Any]) -> str:
    """对任意映射计算规范化 JSON 的 SHA-256 摘要，用于确定性标识与审计哈希。"""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(canonical.encode()).hexdigest()


def _relevance(query: str, content: str) -> int:
    """计算查询与记忆正文的词法相关性：子串命中记 1，否则按词元交集计数。"""
    if not query:
        return 0
    if query.casefold() in content.casefold():
        return 1
    query_terms = _terms(query)
    content_terms = _terms(content)
    return len(query_terms.intersection(content_terms))


def _terms(value: str) -> set[str]:
    """提取词元集合：拉丁字母/数字串原样小写，汉字串拆为相邻二元组。"""
    terms: set[str] = set()
    for term in _TERM.findall(value):
        normalized = term.casefold()
        if any("\u4e00" <= character <= "\u9fff" for character in normalized):
            terms.update(normalized[index : index + 2] for index in range(len(normalized) - 1))
        else:
            terms.add(normalized)
    return terms
