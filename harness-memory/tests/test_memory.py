"""MemoryGateway、InMemory 与 SQLite Provider 的 F3 Gate。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness_contracts import (
    ErrorCode,
    IdentityContext,
    InvocationContext,
    MemoryAccessError,
    MemoryKind,
    MemorySensitivity,
    MemoryWriteDraft,
    Request,
    RequestInput,
    TenantContext,
)
from harness_memory import (
    InMemoryMemoryProvider,
    MemoryGateway,
    MemoryPolicy,
    MemoryProposalConflictError,
    SQLiteMemoryProvider,
)
from harness_policy import (
    AllowAllPolicy,
    Policy,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyPhase,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def invocation(
    request_id: str,
    *,
    tenant: str = "tenant-a",
    subject: str = "subject-a",
) -> InvocationContext:
    return InvocationContext(
        request=Request(
            request_id=request_id,
            tenant_id="untrusted-request-tenant",
            user_id="untrusted-request-user",
            input=RequestInput(type="text", content="remember this"),
        ),
        tenant=TenantContext(tenant_id=tenant),
        identity=IdentityContext(subject=subject),
    )


def draft(
    request_id: str,
    value: str,
    *,
    kind: MemoryKind = MemoryKind.PREFERENCE,
    tags: frozenset[str] = frozenset({"profile"}),
) -> MemoryWriteDraft:
    return MemoryWriteDraft(
        kind=kind,
        content={"value": value},
        tags=tags,
        evidence_refs=(f"request:{request_id}",),
    )


class MemoryEffectPolicy(Policy):
    def __init__(self, phase: PolicyPhase, effect: str) -> None:
        self._phase = phase
        self._effect = effect

    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({self._phase})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if self._effect == "deny":
            return PolicyDecision.deny(self.name, reason="test denied memory")
        return PolicyDecision.require_approval(
            self.name,
            reason="test requested approval",
        )


class MemoryGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.current_time = NOW
        self.provider = InMemoryMemoryProvider()
        self.gateway = MemoryGateway(
            self.provider,
            MemoryPolicy(PolicyEngine((AllowAllPolicy(),))),
            allowed_namespaces={"profile", "conversation"},
            clock=lambda: self.current_time,
        )

    async def test_create_put_is_idempotent_and_conflict_is_stable(self) -> None:
        context = invocation("req-1")
        proposal = self.gateway.create_proposal(
            context,
            draft("req-1", "zh-CN"),
            proposal_id="preference-language",
            namespace="profile",
        )

        first = await self.gateway.put(context, proposal)
        self.current_time += timedelta(minutes=5)
        second = await self.gateway.put(context, proposal)

        self.assertEqual(first, second)
        self.assertEqual(first.created_at, NOW)
        self.assertEqual(await self.provider.count(), 1)
        conflicting = self.gateway.create_proposal(
            context,
            draft("req-1", "en-US"),
            proposal_id="preference-language",
            namespace="profile",
        )
        with self.assertRaises(MemoryProposalConflictError) as raised:
            await self.gateway.put(context, conflicting)
        self.assertEqual(raised.exception.code, ErrorCode.MEMORY_PROPOSAL_CONFLICT)

    async def test_scope_namespace_get_delete_and_trusted_identity(self) -> None:
        owner = invocation("req-owner")
        proposal = self.gateway.create_proposal(
            owner,
            draft("req-owner", "owner-only"),
            proposal_id="owner-memory",
            namespace="profile",
        )
        record = await self.gateway.put(owner, proposal)

        self.assertEqual(record.tenant_id, "tenant-a")
        self.assertEqual(record.subject_id, "subject-a")
        with self.assertRaises(MemoryAccessError) as wrong_subject:
            await self.gateway.get(
                invocation("req-other", subject="subject-b"),
                "profile",
                record.memory_id,
            )
        self.assertEqual(wrong_subject.exception.code, ErrorCode.MEMORY_SCOPE_VIOLATION)
        with self.assertRaises(MemoryAccessError) as wrong_namespace:
            await self.gateway.delete(owner, "conversation", record.memory_id)
        self.assertEqual(wrong_namespace.exception.code, ErrorCode.MEMORY_SCOPE_VIOLATION)

        await self.gateway.delete(owner, "profile", record.memory_id)
        await self.gateway.delete(owner, "profile", record.memory_id)
        self.assertIsNone(await self.gateway.get(owner, "profile", record.memory_id))

    async def test_search_filters_orders_truncates_and_expires(self) -> None:
        context = invocation("req-search")
        first = self.gateway.create_proposal(
            context,
            draft("req-search", "alpha", tags=frozenset({"profile", "chosen"})),
            proposal_id="first",
            namespace="profile",
        )
        await self.gateway.put(context, first)
        self.current_time += timedelta(minutes=1)
        second = self.gateway.create_proposal(
            context,
            draft("req-search", "beta", tags=frozenset({"profile", "chosen"})),
            proposal_id="second",
            namespace="profile",
        )
        second_record = await self.gateway.put(context, second)
        expiring = self.gateway.create_proposal(
            context,
            draft("req-search", "expired", tags=frozenset({"profile", "expiry"})),
            proposal_id="expiring",
            namespace="profile",
            expires_at=self.current_time + timedelta(minutes=1),
        )
        await self.gateway.put(context, expiring)

        query = self.gateway.create_query(
            context,
            namespaces={"profile"},
            tags={"chosen"},
            limit=1,
        )
        result = await self.gateway.search(context, query)
        self.assertEqual(result.records, (second_record,))
        self.assertTrue(result.truncated)

        text_query = self.gateway.create_query(
            context,
            namespaces={"profile"},
            text="BETA",
        )
        self.assertEqual((await self.gateway.search(context, text_query)).records, (second_record,))

        self.current_time += timedelta(minutes=2)
        expired_query = self.gateway.create_query(
            context,
            namespaces={"profile"},
            text="expired",
        )
        self.assertEqual((await self.gateway.search(context, expired_query)).records, ())

    async def test_evidence_sensitivity_size_and_limits_fail_closed(self) -> None:
        context = invocation("req-guard")
        with self.assertRaises(MemoryAccessError) as evidence:
            self.gateway.create_proposal(
                context,
                draft("other-request", "unsupported"),
                proposal_id="bad-evidence",
                namespace="profile",
            )
        self.assertEqual(evidence.exception.code, ErrorCode.MEMORY_EVIDENCE_INVALID)

        secret = self.gateway.create_proposal(
            context,
            draft("req-guard", "secret"),
            proposal_id="secret",
            namespace="profile",
            sensitivity=MemorySensitivity.SECRET,
        )
        with self.assertRaises(MemoryAccessError) as secret_error:
            await self.gateway.put(context, secret)
        self.assertEqual(secret_error.exception.code, ErrorCode.MEMORY_INVALID)

        small_gateway = MemoryGateway(
            InMemoryMemoryProvider(),
            MemoryPolicy(PolicyEngine((AllowAllPolicy(),))),
            allowed_namespaces={"profile"},
            max_record_bytes=256,
            max_slice_bytes=256,
            clock=lambda: NOW,
        )
        oversized = small_gateway.create_proposal(
            context,
            draft("req-guard", "x" * 512),
            proposal_id="oversized",
            namespace="profile",
        )
        with self.assertRaises(MemoryAccessError) as too_large:
            await small_gateway.put(context, oversized)
        self.assertEqual(too_large.exception.code, ErrorCode.MEMORY_TOO_LARGE)
        with self.assertRaises(ValueError):
            MemoryGateway(
                InMemoryMemoryProvider(),
                MemoryPolicy(PolicyEngine((AllowAllPolicy(),))),
                allowed_namespaces={"profile"},
                max_record_bytes=32 * 1024 + 1,
            )

    async def test_slice_byte_limit_stably_trims_provider_results(self) -> None:
        context = invocation("req-slice")
        gateway = MemoryGateway(
            InMemoryMemoryProvider(),
            MemoryPolicy(PolicyEngine((AllowAllPolicy(),))),
            allowed_namespaces={"profile"},
            max_record_bytes=4096,
            max_slice_bytes=1800,
            clock=lambda: NOW,
        )
        for index in range(2):
            proposal = gateway.create_proposal(
                context,
                draft("req-slice", f"{index}-" + "x" * 700),
                proposal_id=f"slice-{index}",
                namespace="profile",
            )
            await gateway.put(context, proposal)

        query = gateway.create_query(context, namespaces={"profile"})
        first = await gateway.search(context, query)
        second = await gateway.search(context, query)

        self.assertEqual(first, second)
        self.assertEqual(len(first.records), 1)
        self.assertTrue(first.truncated)

    async def test_memory_policy_deny_and_approval_are_stable(self) -> None:
        context = invocation("req-policy")
        denied_gateway = MemoryGateway(
            InMemoryMemoryProvider(),
            MemoryPolicy(PolicyEngine((MemoryEffectPolicy(PolicyPhase.PRE_MEMORY_WRITE, "deny"),))),
            allowed_namespaces={"profile"},
            clock=lambda: NOW,
        )
        proposal = denied_gateway.create_proposal(
            context,
            draft("req-policy", "denied"),
            proposal_id="denied",
            namespace="profile",
        )
        with self.assertRaises(MemoryAccessError) as denied:
            await denied_gateway.put(context, proposal)
        self.assertEqual(denied.exception.code, ErrorCode.MEMORY_POLICY_DENIED)

        approval_gateway = MemoryGateway(
            InMemoryMemoryProvider(),
            MemoryPolicy(
                PolicyEngine((MemoryEffectPolicy(PolicyPhase.PRE_MEMORY_READ, "approval"),))
            ),
            allowed_namespaces={"profile"},
            clock=lambda: NOW,
        )
        query = approval_gateway.create_query(context, namespaces={"profile"})
        with self.assertRaises(MemoryAccessError) as approval:
            await approval_gateway.search(context, query)
        self.assertEqual(approval.exception.code, ErrorCode.MEMORY_POLICY_UNSUPPORTED)

        shared_provider = InMemoryMemoryProvider()
        allowed_gateway = MemoryGateway(
            shared_provider,
            MemoryPolicy(PolicyEngine((AllowAllPolicy(),))),
            allowed_namespaces={"profile"},
            clock=lambda: NOW,
        )
        deletable = allowed_gateway.create_proposal(
            context,
            draft("req-policy", "delete-governed"),
            proposal_id="delete-governed",
            namespace="profile",
        )
        stored = await allowed_gateway.put(context, deletable)
        delete_denied_gateway = MemoryGateway(
            shared_provider,
            MemoryPolicy(
                PolicyEngine((MemoryEffectPolicy(PolicyPhase.PRE_MEMORY_DELETE, "deny"),))
            ),
            allowed_namespaces={"profile"},
            clock=lambda: NOW,
        )
        with self.assertRaises(MemoryAccessError) as delete_denied:
            await delete_denied_gateway.delete(context, "profile", stored.memory_id)
        self.assertEqual(delete_denied.exception.code, ErrorCode.MEMORY_POLICY_DENIED)
        self.assertEqual(await allowed_gateway.get(context, "profile", stored.memory_id), stored)


class SQLiteMemoryProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_across_instances_and_preserves_create_only_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "memory.sqlite3"
            policy = MemoryPolicy(PolicyEngine((AllowAllPolicy(),)))
            first = MemoryGateway(
                SQLiteMemoryProvider(database),
                policy,
                allowed_namespaces={"profile"},
                clock=lambda: NOW,
            )
            context = invocation("req-sqlite")
            proposal = first.create_proposal(
                context,
                draft("req-sqlite", "persistent"),
                proposal_id="persistent-memory",
                namespace="profile",
            )
            written = await first.put(context, proposal)

            second = MemoryGateway(
                SQLiteMemoryProvider(database),
                policy,
                allowed_namespaces={"profile"},
                clock=lambda: NOW + timedelta(hours=1),
            )
            self.assertEqual(
                await second.get(context, "profile", written.memory_id),
                written,
            )
            self.assertEqual(await second.put(context, proposal), written)
            filtered = second.create_query(
                context,
                namespaces={"profile"},
                tags={"profile"},
                text="PERSISTENT",
            )
            self.assertEqual((await second.search(context, filtered)).records, (written,))
            conflict = second.create_proposal(
                context,
                draft("req-sqlite", "different"),
                proposal_id="persistent-memory",
                namespace="profile",
            )
            with self.assertRaises(MemoryProposalConflictError):
                await second.put(context, conflict)

            await second.delete(context, "profile", written.memory_id)
            self.assertIsNone(await first.get(context, "profile", written.memory_id))


if __name__ == "__main__":
    unittest.main()
