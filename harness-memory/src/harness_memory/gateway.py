"""MemoryProvider 外层唯一授权、治理、大小与 identity 边界。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from harness_contracts import (
    ErrorCode,
    InvocationContext,
    MemoryAccessError,
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    MemorySensitivity,
    MemorySlice,
    MemorySubjectScope,
    MemoryWriteDraft,
    MemoryWriteProposal,
)

from .canonical import (
    canonical_hash,
    canonical_size,
    matches_query,
    memory_id_for,
    proposal_facts,
    proposal_hash,
    query_hash,
    record_order,
)
from .errors import MemoryProposalConflictError, MemoryProviderError
from .evidence import MemoryEvidenceResolver, RequestEvidenceResolver
from .policy import MemoryPolicy
from .provider import MemoryProvider, validate_memory_id

type Clock = Callable[[], datetime]

MAX_MEMORY_RECORD_BYTES = 32 * 1024
MAX_MEMORY_SLICE_BYTES = 128 * 1024


class MemoryGateway:
    def __init__(
        self,
        provider: MemoryProvider,
        policy: MemoryPolicy,
        *,
        allowed_namespaces: Iterable[str],
        evidence_resolver: MemoryEvidenceResolver | None = None,
        max_record_bytes: int = MAX_MEMORY_RECORD_BYTES,
        max_slice_bytes: int = MAX_MEMORY_SLICE_BYTES,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(provider, MemoryProvider):
            raise TypeError("provider must implement MemoryProvider")
        if not isinstance(policy, MemoryPolicy):
            raise TypeError("policy must be MemoryPolicy")
        if isinstance(allowed_namespaces, str):
            raise TypeError("allowed_namespaces must be an iterable of strings")
        namespaces = frozenset(allowed_namespaces)
        if not namespaces or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in namespaces
        ):
            raise ValueError("allowed_namespaces must contain non-empty trimmed strings")
        if evidence_resolver is not None and not isinstance(
            evidence_resolver,
            MemoryEvidenceResolver,
        ):
            raise TypeError("evidence_resolver must implement MemoryEvidenceResolver")
        _validate_size_limit(
            "max_record_bytes",
            max_record_bytes,
            maximum=MAX_MEMORY_RECORD_BYTES,
        )
        _validate_size_limit(
            "max_slice_bytes",
            max_slice_bytes,
            maximum=MAX_MEMORY_SLICE_BYTES,
        )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")

        self._provider = provider
        self._policy = policy
        self._allowed_namespaces = namespaces
        self._evidence_resolver = evidence_resolver or RequestEvidenceResolver()
        self._max_record_bytes = max_record_bytes
        self._max_slice_bytes = max_slice_bytes
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def provider(self) -> MemoryProvider:
        return self._provider

    @property
    def policy(self) -> MemoryPolicy:
        return self._policy

    @property
    def allowed_namespaces(self) -> frozenset[str]:
        return self._allowed_namespaces

    def subject_scope(self, invocation: InvocationContext) -> MemorySubjectScope:
        if not isinstance(invocation, InvocationContext):
            raise TypeError("invocation must be InvocationContext")
        tenant = invocation.tenant
        identity = invocation.identity
        if tenant is None or identity is None:
            raise MemoryAccessError(
                "memory requires trusted tenant and identity context",
                code=ErrorCode.MEMORY_TRUSTED_SCOPE_REQUIRED,
            )
        return MemorySubjectScope(
            tenant_id=tenant.tenant_id,
            subject_id=identity.subject,
        )

    def create_query(
        self,
        invocation: InvocationContext,
        *,
        namespaces: Iterable[str],
        kinds: Iterable[MemoryKind] | None = None,
        tags: Iterable[str] = (),
        text: str | None = None,
        limit: int = 20,
    ) -> MemoryQuery:
        scope = self.subject_scope(invocation)
        return MemoryQuery(
            tenant_id=scope.tenant_id,
            subject_id=scope.subject_id,
            namespaces=frozenset(namespaces),
            kinds=frozenset(kinds) if kinds is not None else frozenset(MemoryKind),
            tags=frozenset(tags),
            text=text,
            limit=limit,
        )

    def create_proposal(
        self,
        invocation: InvocationContext,
        draft: MemoryWriteDraft,
        *,
        proposal_id: str,
        namespace: str,
        sensitivity: MemorySensitivity = MemorySensitivity.INTERNAL,
        expires_at: datetime | None = None,
        producer: str = "harness-memory.gateway",
    ) -> MemoryWriteProposal:
        if not isinstance(draft, MemoryWriteDraft):
            raise TypeError("draft must be MemoryWriteDraft")
        scope = self.subject_scope(invocation)
        self._require_namespace(namespace)
        if not self._evidence_resolver.resolves(invocation, draft.evidence_refs):
            raise MemoryAccessError(
                "memory draft evidence cannot be resolved",
                code=ErrorCode.MEMORY_EVIDENCE_INVALID,
            )
        source_fact_hash = canonical_hash(
            {
                "content": draft.model_dump(mode="json")["content"],
                "evidence_refs": list(draft.evidence_refs),
            }
        )
        base = {
            "proposal_id": proposal_id,
            "tenant_id": scope.tenant_id,
            "subject_id": scope.subject_id,
            "namespace": namespace,
            "kind": draft.kind,
            "content": draft.content,
            "tags": draft.tags,
            "sensitivity": sensitivity,
            "evidence_refs": draft.evidence_refs,
            "source_fact_hash": source_fact_hash,
            "provenance": MemoryProvenance(
                producer=producer,
                source_fact_hash=source_fact_hash,
                evidence_refs=draft.evidence_refs,
            ),
            "expires_at": expires_at,
        }
        provisional = MemoryWriteProposal(proposal_hash="0" * 64, **base)
        return MemoryWriteProposal(
            proposal_hash=canonical_hash(proposal_facts(provisional)),
            **base,
        )

    async def get(
        self,
        invocation: InvocationContext,
        namespace: str,
        memory_id: str,
    ) -> MemoryRecord | None:
        scope = self.subject_scope(invocation)
        self._require_namespace(namespace)
        validate_memory_id(memory_id)
        record = await self._provider_get(memory_id)
        if record is None:
            return None
        self._require_record_valid(record)
        self._require_record_scope(record, scope, namespace=namespace)
        if not self._policy.allows_read(invocation, scope, record=record):
            raise MemoryAccessError(
                "memory read denied by policy",
                code=ErrorCode.MEMORY_POLICY_DENIED,
                details={"operation": "get"},
            )
        return None if self._is_expired(record) else record

    async def search(
        self,
        invocation: InvocationContext,
        query: MemoryQuery,
    ) -> MemorySlice:
        if not isinstance(query, MemoryQuery):
            raise TypeError("query must be MemoryQuery")
        scope = self.subject_scope(invocation)
        self._require_query_scope(query, scope)
        for namespace in query.namespaces:
            self._require_namespace(namespace)
        if not self._policy.allows_read(invocation, scope, query=query):
            raise MemoryAccessError(
                "memory search denied by policy",
                code=ErrorCode.MEMORY_POLICY_DENIED,
                details={"operation": "search"},
            )

        provided = await self._provider_search(query)
        records_by_id: dict[str, MemoryRecord] = {}
        truncated = False
        for record in provided:
            if not isinstance(record, MemoryRecord):
                raise MemoryAccessError(
                    "memory provider returned an invalid record",
                    code=ErrorCode.MEMORY_PROVIDER_INVALID,
                )
            self._require_record_scope(record, scope)
            if not matches_query(record, query):
                truncated = True
                continue
            if self._is_expired(record):
                continue
            if _record_size(record) > self._max_record_bytes:
                truncated = True
                continue
            if record.sensitivity is MemorySensitivity.SECRET:
                truncated = True
                continue
            if not self._policy.allows_read(invocation, scope, record=record):
                continue
            previous = records_by_id.get(record.memory_id)
            if previous is not None and previous != record:
                raise MemoryAccessError(
                    "memory provider returned conflicting duplicate identities",
                    code=ErrorCode.MEMORY_PROVIDER_INVALID,
                    details={"memory_id": record.memory_id},
                )
            records_by_id[record.memory_id] = record

        ordered = tuple(sorted(records_by_id.values(), key=record_order))
        included: list[MemoryRecord] = []
        stable_query_hash = query_hash(query)
        for record in ordered:
            if len(included) >= query.limit:
                truncated = True
                break
            candidate = (*included, record)
            candidate_slice = MemorySlice(
                records=candidate,
                query_hash=stable_query_hash,
                truncated=True,
            )
            if canonical_size(candidate_slice.model_dump(mode="json")) > self._max_slice_bytes:
                truncated = True
                break
            included.append(record)
        return MemorySlice(
            records=tuple(included),
            query_hash=stable_query_hash,
            truncated=truncated,
        )

    async def put(
        self,
        invocation: InvocationContext,
        proposal: MemoryWriteProposal,
    ) -> MemoryRecord:
        if not isinstance(proposal, MemoryWriteProposal):
            raise TypeError("proposal must be MemoryWriteProposal")
        scope = self.subject_scope(invocation)
        now = self._now()
        self._require_proposal_valid(invocation, proposal, scope, now=now)
        self._policy.require_write(invocation, scope, proposal)
        record = MemoryRecord(
            memory_id=memory_id_for(proposal),
            tenant_id=scope.tenant_id,
            subject_id=scope.subject_id,
            namespace=proposal.namespace,
            kind=proposal.kind,
            content=proposal.content,
            tags=proposal.tags,
            sensitivity=proposal.sensitivity,
            provenance=proposal.provenance,
            created_at=now,
            updated_at=now,
            expires_at=proposal.expires_at,
        )
        if _record_size(record) > self._max_record_bytes:
            raise MemoryAccessError(
                "memory record exceeds the configured size limit",
                code=ErrorCode.MEMORY_TOO_LARGE,
            )
        stored = await self._provider_put(record, proposal.proposal_hash)
        self._validate_stored_record(stored, proposal, record.memory_id)
        return stored

    async def delete(
        self,
        invocation: InvocationContext,
        namespace: str,
        memory_id: str,
    ) -> None:
        scope = self.subject_scope(invocation)
        self._require_namespace(namespace)
        validate_memory_id(memory_id)
        record = await self._provider_get(memory_id)
        if record is None:
            return
        self._require_record_valid(record)
        self._require_record_scope(record, scope, namespace=namespace)
        self._policy.require_delete(invocation, scope, record)
        await self._provider_delete(memory_id)

    def _require_proposal_valid(
        self,
        invocation: InvocationContext,
        proposal: MemoryWriteProposal,
        scope: MemorySubjectScope,
        *,
        now: datetime,
    ) -> None:
        self._require_namespace(proposal.namespace)
        if proposal.tenant_id != scope.tenant_id or proposal.subject_id != scope.subject_id:
            raise MemoryAccessError(
                "memory proposal does not match trusted scope",
                code=ErrorCode.MEMORY_SCOPE_VIOLATION,
            )
        if proposal_hash(proposal) != proposal.proposal_hash:
            raise MemoryAccessError(
                "memory proposal hash does not match canonical facts",
                code=ErrorCode.MEMORY_INVALID,
                details={"reason": "proposal_hash_mismatch"},
            )
        if proposal.sensitivity is MemorySensitivity.SECRET:
            raise MemoryAccessError(
                "secret values cannot be persisted as memory",
                code=ErrorCode.MEMORY_INVALID,
                details={"reason": "secret_memory_forbidden"},
            )
        if not self._evidence_resolver.resolves(invocation, proposal.evidence_refs):
            raise MemoryAccessError(
                "memory proposal evidence cannot be resolved",
                code=ErrorCode.MEMORY_EVIDENCE_INVALID,
            )
        if canonical_size(proposal_facts(proposal)) > self._max_record_bytes:
            raise MemoryAccessError(
                "memory proposal exceeds the configured size limit",
                code=ErrorCode.MEMORY_TOO_LARGE,
            )
        if proposal.expires_at is not None and proposal.expires_at <= now:
            raise MemoryAccessError(
                "memory proposal retention has already expired",
                code=ErrorCode.MEMORY_INVALID,
                details={"reason": "expired_retention"},
            )

    def _require_query_scope(
        self,
        query: MemoryQuery,
        scope: MemorySubjectScope,
    ) -> None:
        if query.tenant_id != scope.tenant_id or query.subject_id != scope.subject_id:
            raise MemoryAccessError(
                "memory query does not match trusted scope",
                code=ErrorCode.MEMORY_SCOPE_VIOLATION,
            )

    def _require_record_scope(
        self,
        record: MemoryRecord,
        scope: MemorySubjectScope,
        *,
        namespace: str | None = None,
    ) -> None:
        if record.tenant_id != scope.tenant_id or record.subject_id != scope.subject_id:
            raise MemoryAccessError(
                "memory record does not match trusted scope",
                code=ErrorCode.MEMORY_SCOPE_VIOLATION,
            )
        if namespace is not None and record.namespace != namespace:
            raise MemoryAccessError(
                "memory record does not match requested namespace",
                code=ErrorCode.MEMORY_SCOPE_VIOLATION,
            )
        self._require_namespace(record.namespace)

    def _require_record_valid(self, record: MemoryRecord) -> None:
        if not isinstance(record, MemoryRecord):
            raise MemoryAccessError(
                "memory provider returned an invalid record",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
            )
        if record.sensitivity is MemorySensitivity.SECRET:
            raise MemoryAccessError(
                "memory provider returned secret content",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
            )
        if _record_size(record) > self._max_record_bytes:
            raise MemoryAccessError(
                "memory provider returned an oversized record",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
            )

    def _validate_stored_record(
        self,
        stored: MemoryRecord,
        proposal: MemoryWriteProposal,
        expected_memory_id: str,
    ) -> None:
        self._require_record_valid(stored)
        expected = {
            "content": proposal.content,
            "expires_at": proposal.expires_at,
            "kind": proposal.kind,
            "memory_id": expected_memory_id,
            "namespace": proposal.namespace,
            "provenance": proposal.provenance,
            "sensitivity": proposal.sensitivity,
            "subject_id": proposal.subject_id,
            "tags": proposal.tags,
            "tenant_id": proposal.tenant_id,
        }
        actual = {key: getattr(stored, key) for key in expected}
        if actual != expected:
            raise MemoryAccessError(
                "memory provider returned a record that does not match the proposal",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
            )

    def _require_namespace(self, namespace: str) -> None:
        if not isinstance(namespace, str) or namespace not in self._allowed_namespaces:
            raise MemoryAccessError(
                "memory namespace is not allowed",
                code=ErrorCode.MEMORY_NAMESPACE_NOT_ALLOWED,
                details={"namespace": str(namespace)},
            )

    def _is_expired(self, record: MemoryRecord) -> bool:
        return record.expires_at is not None and record.expires_at <= self._now()

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise TypeError("memory clock must return a timezone-aware datetime")
        return now

    async def _provider_get(self, memory_id: str) -> MemoryRecord | None:
        try:
            return await self._provider.get(memory_id)
        except MemoryAccessError:
            raise
        except Exception as exc:
            raise MemoryProviderError(
                "memory provider get failed",
                details={"cause_type": type(exc).__name__},
            ) from exc

    async def _provider_search(self, query: MemoryQuery) -> tuple[MemoryRecord, ...]:
        try:
            records = await self._provider.search(query)
        except MemoryAccessError:
            raise
        except Exception as exc:
            raise MemoryProviderError(
                "memory provider search failed",
                details={"cause_type": type(exc).__name__},
            ) from exc
        if not isinstance(records, tuple):
            raise MemoryAccessError(
                "memory provider search must return a tuple",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
            )
        return records

    async def _provider_put(
        self,
        record: MemoryRecord,
        stable_proposal_hash: str,
    ) -> MemoryRecord:
        try:
            stored = await self._provider.put_if_absent(record, stable_proposal_hash)
        except MemoryProposalConflictError:
            raise
        except MemoryAccessError:
            raise
        except Exception as exc:
            raise MemoryProviderError(
                "memory provider put failed",
                details={"cause_type": type(exc).__name__},
            ) from exc
        if not isinstance(stored, MemoryRecord):
            raise MemoryAccessError(
                "memory provider put returned an invalid record",
                code=ErrorCode.MEMORY_PROVIDER_INVALID,
            )
        return stored

    async def _provider_delete(self, memory_id: str) -> None:
        try:
            await self._provider.delete(memory_id)
        except MemoryAccessError:
            raise
        except Exception as exc:
            raise MemoryProviderError(
                "memory provider delete failed",
                details={"cause_type": type(exc).__name__},
            ) from exc


def _record_size(record: MemoryRecord) -> int:
    payload = record.model_dump(mode="json")
    payload["tags"] = sorted(record.tags)
    return canonical_size(payload)


def _validate_size_limit(field_name: str, value: int, *, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TypeError(f"{field_name} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum}")
