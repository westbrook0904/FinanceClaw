"""PlanValidator 的结构与可执行性校验测试。"""

from __future__ import annotations

import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    ConditionExpr,
    ConditionOperator,
    ExecutionPlan,
    FailurePolicy,
    InvocationContext,
    NodeOutputBinding,
    PlanEdge,
    PlanNode,
    PlanNodeKind,
    RequestBinding,
    ResultEnvelope,
    RetryPolicy,
    ValueReference,
)
from harness_planning import PlanValidationCode, PlanValidationError, PlanValidator
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_spi import ToolRequest, ToolSPI


class StubTool(ToolSPI):
    def __init__(self, capability_id: str) -> None:
        self._descriptor = CapabilityDescriptor(
            id=capability_id,
            name=capability_id,
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        raise NotImplementedError


def make_catalog(*capability_ids: str) -> RegistryCapabilityCatalog:
    registry = InMemoryCapabilityRegistry()
    for capability_id in capability_ids:
        registry.register(StubTool(capability_id), plugin_id="test-plugin")
    return RegistryCapabilityCatalog(registry)


def make_valid_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-001",
        nodes=(
            PlanNode(
                node_id="n1",
                capability="data.fetch/v1",
                input_mapping={"query": RequestBinding(pointer="/input/content")},
            ),
            PlanNode(
                node_id="n2",
                capability="data.rank/v1",
                input_mapping={
                    "items": NodeOutputBinding(
                        node_id="n1",
                        pointer="/output/data/items",
                    )
                },
            ),
        ),
        edges=(
            PlanEdge(
                from_node="n1",
                to_node="n2",
                condition=ConditionExpr(
                    operator=ConditionOperator.EXISTS,
                    ref=ValueReference(
                        node_id="n1",
                        pointer="/output/data/items",
                    ),
                ),
            ),
        ),
        outputs={
            "ranking": NodeOutputBinding(
                node_id="n2",
                pointer="/output/data",
            )
        },
    )


def issue_codes(error: PlanValidationError) -> set[PlanValidationCode]:
    return {issue.code for issue in error.issues}


class PlanValidatorTests(unittest.TestCase):
    def test_valid_dag_passes_structural_and_catalog_validation(self) -> None:
        validator = PlanValidator(make_catalog("data.fetch/v1", "data.rank/v1"))
        plan = make_valid_plan()

        validated = validator.validate(plan)

        self.assertIs(validated, plan)
        self.assertEqual(validator.find_issues(plan), ())

    def test_cycle_and_missing_edge_endpoints_are_reported(self) -> None:
        cycle = ExecutionPlan(
            plan_id="cycle",
            nodes=(
                PlanNode(node_id="n1", capability="a/v1"),
                PlanNode(node_id="n2", capability="b/v1"),
            ),
            edges=(
                PlanEdge(from_node="n1", to_node="n2"),
                PlanEdge(from_node="n2", to_node="n1"),
                PlanEdge(from_node="missing", to_node="n1"),
                PlanEdge(from_node="n1", to_node="other"),
            ),
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(cycle)

        codes = issue_codes(raised.exception)
        self.assertIn(PlanValidationCode.CYCLE, codes)
        self.assertIn(PlanValidationCode.NO_ROOT, codes)
        self.assertIn(PlanValidationCode.EDGE_SOURCE_NOT_FOUND, codes)
        self.assertIn(PlanValidationCode.EDGE_TARGET_NOT_FOUND, codes)

    def test_dangling_and_unavailable_bindings_are_reported(self) -> None:
        plan = ExecutionPlan(
            plan_id="bindings",
            nodes=(
                PlanNode(node_id="n1", capability="a/v1"),
                PlanNode(
                    node_id="n2",
                    capability="b/v1",
                    input_mapping={
                        "parallel": NodeOutputBinding(node_id="n1", pointer="/output/data"),
                        "missing": NodeOutputBinding(node_id="missing", pointer="/output/data"),
                        "bad_request": RequestBinding(pointer="/unknown/value"),
                    },
                ),
            ),
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan)

        codes = issue_codes(raised.exception)
        self.assertIn(PlanValidationCode.INPUT_REFERENCE_UNAVAILABLE, codes)
        self.assertIn(PlanValidationCode.INPUT_REFERENCE_NOT_FOUND, codes)
        self.assertIn(PlanValidationCode.INVALID_BINDING, codes)

    def test_output_and_condition_references_are_checked(self) -> None:
        plan = ExecutionPlan(
            plan_id="references",
            nodes=(
                PlanNode(node_id="n1", capability="a/v1"),
                PlanNode(node_id="n2", capability="b/v1"),
            ),
            edges=(
                PlanEdge(
                    from_node="n1",
                    to_node="n2",
                    condition=ConditionExpr(
                        operator=ConditionOperator.EQ,
                        ref=ValueReference(
                            node_id="missing",
                            pointer="/not-a-result/value",
                        ),
                        value=True,
                    ),
                ),
            ),
            outputs={
                "missing": NodeOutputBinding(
                    node_id="missing",
                    pointer="/not-a-result/value",
                )
            },
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan)

        codes = issue_codes(raised.exception)
        self.assertIn(PlanValidationCode.CONDITION_REFERENCE_NOT_FOUND, codes)
        self.assertIn(PlanValidationCode.INVALID_CONDITION, codes)
        self.assertIn(PlanValidationCode.OUTPUT_REFERENCE_NOT_FOUND, codes)
        self.assertIn(PlanValidationCode.INVALID_OUTPUT, codes)

    def test_contract_bypasses_are_defensively_rejected(self) -> None:
        invalid_capability = PlanNode.model_construct(
            node_id="n1",
            kind=PlanNodeKind.CAPABILITY,
            capability=None,
            input_mapping={},
            timeout_ms=0,
            retry_policy=RetryPolicy.model_construct(
                max_attempts=0,
                initial_backoff_ms=100,
                max_backoff_ms=10,
                multiplier=0.5,
            ),
            failure_policy=FailurePolicy.FAIL_FAST,
            idempotency_key=None,
            policy_tags=frozenset(),
            metadata={},
        )
        invalid_approval = PlanNode.model_construct(
            node_id="approval",
            kind=PlanNodeKind.APPROVAL,
            capability="must-not-exist/v1",
            input_mapping={},
            timeout_ms=None,
            retry_policy=RetryPolicy(),
            failure_policy=FailurePolicy.FAIL_PLAN,
            idempotency_key=None,
            policy_tags=frozenset(),
            metadata={},
        )
        plan = ExecutionPlan.model_construct(
            plan_id="defensive",
            revision=1,
            budget=make_valid_plan().budget,
            failure_policy=FailurePolicy.FAIL_FAST,
            nodes=(invalid_capability, invalid_approval),
            edges=(),
            outputs={},
            metadata={},
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan)

        codes = issue_codes(raised.exception)
        self.assertIn(PlanValidationCode.INVALID_CAPABILITY_NODE, codes)
        self.assertIn(PlanValidationCode.INVALID_APPROVAL_NODE, codes)
        self.assertIn(PlanValidationCode.INVALID_TIMEOUT, codes)
        self.assertIn(PlanValidationCode.INVALID_RETRY_POLICY, codes)
        self.assertIn(PlanValidationCode.INVALID_FAILURE_POLICY, codes)

    def test_duplicate_node_ids_are_defensively_rejected(self) -> None:
        node = PlanNode(node_id="n1", capability="a/v1")
        plan = ExecutionPlan.model_construct(
            plan_id="duplicate",
            revision=1,
            budget=make_valid_plan().budget,
            failure_policy=FailurePolicy.FAIL_FAST,
            nodes=(node, node),
            edges=(),
            outputs={},
            metadata={},
        )

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan)

        self.assertIn(
            PlanValidationCode.DUPLICATE_NODE_ID,
            issue_codes(raised.exception),
        )

    def test_approval_node_is_valid_without_catalog_entry(self) -> None:
        plan = ExecutionPlan(
            plan_id="approval",
            nodes=(PlanNode(node_id="approve", kind=PlanNodeKind.APPROVAL),),
        )

        self.assertIs(PlanValidator(make_catalog()).validate(plan), plan)

    def test_catalog_validation_can_be_enabled_or_skipped(self) -> None:
        plan = ExecutionPlan(
            plan_id="catalog",
            nodes=(PlanNode(node_id="n1", capability="missing/v1"),),
        )
        validator = PlanValidator(make_catalog())

        with self.assertRaises(PlanValidationError) as raised:
            validator.validate(plan)
        self.assertIn(
            PlanValidationCode.CAPABILITY_NOT_FOUND,
            issue_codes(raised.exception),
        )
        self.assertIs(validator.validate(plan, executable=False), plan)

    def test_empty_plan_is_rejected(self) -> None:
        plan = ExecutionPlan(plan_id="empty", nodes=())

        with self.assertRaises(PlanValidationError) as raised:
            PlanValidator().validate(plan)

        self.assertIn(PlanValidationCode.EMPTY_PLAN, issue_codes(raised.exception))


if __name__ == "__main__":
    unittest.main()
