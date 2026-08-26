"""阶段二 Policy phase/effect 聚合规则测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ExecutionPlan,
    InvocationContext,
    PlanNode,
    Request,
    RequestInput,
)
from harness_policy import (
    Policy,
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyPhase,
)


class PrePlanAllow(Policy):
    @property
    def phases(self) -> frozenset[PolicyPhase]:
        return frozenset({PolicyPhase.PRE_PLAN})

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision.allow(self.name, constraints={"checked": True})


class ApprovalThenDeny(Policy):
    def __init__(self, effect: PolicyEffect) -> None:
        self.effect = effect

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if self.effect is PolicyEffect.DENY:
            return PolicyDecision.deny(self.name, reason="denied")
        return PolicyDecision.require_approval(self.name, reason="approval needed")


class Stage2PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = Request(
            request_id="policy-request",
            input=RequestInput(type="json", content={}),
        )
        self.context = InvocationContext(request=self.request)
        self.capability = CapabilityDescriptor(
            id="tool/v1",
            name="tool",
            type=CapabilityType.TOOL,
            version="1.0.0",
        )
        self.plan = ExecutionPlan(
            plan_id="policy-plan",
            nodes=(PlanNode(node_id="work", capability="tool/v1"),),
        )

    def test_phase_filter_keeps_stage1_policy_out_of_pre_plan(self) -> None:
        engine = PolicyEngine((ApprovalThenDeny(PolicyEffect.DENY), PrePlanAllow()))
        result = engine.evaluate(
            PolicyContext(
                invocation=self.context,
                phase=PolicyPhase.PRE_PLAN,
                plan=self.plan,
            )
        )
        self.assertEqual(result.effect, PolicyEffect.ALLOW)
        self.assertTrue(result.constraints["checked"])

    def test_deny_has_priority_over_require_approval(self) -> None:
        engine = PolicyEngine(
            (
                ApprovalThenDeny(PolicyEffect.REQUIRE_APPROVAL),
                ApprovalThenDeny(PolicyEffect.DENY),
            )
        )
        result = engine.evaluate(
            PolicyContext(
                invocation=self.context,
                phase=PolicyPhase.PRE_EXECUTE,
                capability=self.capability,
            )
        )
        self.assertEqual(result.effect, PolicyEffect.DENY)
