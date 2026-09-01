"""PRE_MEMORY PolicyContext 的类型化边界。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    IdentityContext,
    InvocationContext,
    MemoryQuery,
    MemorySubjectScope,
    Request,
    RequestInput,
    TenantContext,
)
from harness_policy import PolicyContext, PolicyPhase
from pydantic import ValidationError


class MemoryPolicyContextTests(unittest.TestCase):
    def test_read_requires_exactly_one_scoped_target(self) -> None:
        invocation = InvocationContext(
            request=Request(input=RequestInput(type="text", content="query")),
            tenant=TenantContext(tenant_id="tenant-a"),
            identity=IdentityContext(subject="subject-a"),
        )
        scope = MemorySubjectScope(tenant_id="tenant-a", subject_id="subject-a")
        query = MemoryQuery(
            tenant_id="tenant-a",
            subject_id="subject-a",
            namespaces={"profile"},
        )

        context = PolicyContext(
            invocation=invocation,
            phase=PolicyPhase.PRE_MEMORY_READ,
            memory_scope=scope,
            memory_query=query,
        )
        self.assertEqual(context.memory_query, query)
        with self.assertRaises(ValidationError):
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_MEMORY_READ,
                memory_scope=scope,
            )
        with self.assertRaises(ValidationError):
            PolicyContext(
                invocation=invocation,
                phase=PolicyPhase.PRE_MEMORY_READ,
                memory_scope=scope,
                memory_query=query.model_copy(update={"subject_id": "subject-b"}),
            )


if __name__ == "__main__":
    unittest.main()
