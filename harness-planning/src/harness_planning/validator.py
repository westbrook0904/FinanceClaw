"""ExecutionPlan 的确定性结构与可执行性校验。"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable

from harness_contracts import (
    CapabilityType,
    ConditionExpr,
    ConditionOperator,
    ExecutionPlan,
    FailurePolicy,
    LiteralBinding,
    NodeOutputBinding,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestBinding,
    ResultEnvelope,
    RetryPolicy,
    ValueReference,
)
from harness_registry import CapabilityCatalog

from .identity import PlanTemplate
from .models import PlanValidationCode, PlanValidationError, PlanValidationIssue

type PlanShape = ExecutionPlan | PlanTemplate


class PlanValidator:
    """在计划进入 Scheduler 前聚合并报告确定性的协议问题。"""

    def __init__(self, catalog: CapabilityCatalog | None = None) -> None:
        if catalog is not None and not isinstance(catalog, CapabilityCatalog):
            raise TypeError("catalog must implement CapabilityCatalog")
        self._catalog = catalog

    @property
    def catalog(self) -> CapabilityCatalog | None:
        return self._catalog

    def validate(
        self,
        plan: ExecutionPlan,
        *,
        executable: bool = True,
    ) -> ExecutionPlan:
        """返回合法 Plan；存在问题时一次性抛出 ``PlanValidationError``。"""

        issues = self.find_issues(plan, executable=executable)
        if issues:
            raise PlanValidationError(issues)
        return plan

    def validate_template(
        self,
        template: PlanTemplate,
        *,
        executable: bool = True,
    ) -> PlanTemplate:
        """校验 identity-free 模板，不制造 throwaway ``plan_id``。"""

        issues = self.find_template_issues(template, executable=executable)
        if issues:
            raise PlanValidationError(issues)
        return template

    def find_issues(
        self,
        plan: ExecutionPlan,
        *,
        executable: bool = True,
    ) -> tuple[PlanValidationIssue, ...]:
        """执行无副作用校验并返回顺序稳定的问题快照。"""

        if not isinstance(plan, ExecutionPlan):
            raise TypeError("plan must be ExecutionPlan")

        return self._find_shape_issues(plan, executable=executable, validate_identity=True)

    def find_template_issues(
        self,
        template: PlanTemplate,
        *,
        executable: bool = True,
    ) -> tuple[PlanValidationIssue, ...]:
        """返回模板的确定性结构与可执行性问题。"""

        if not isinstance(template, PlanTemplate):
            raise TypeError("template must be PlanTemplate")
        return self._find_shape_issues(
            template,
            executable=executable,
            validate_identity=False,
        )

    def _find_shape_issues(
        self,
        plan: PlanShape,
        *,
        executable: bool,
        validate_identity: bool,
    ) -> tuple[PlanValidationIssue, ...]:
        issues: list[PlanValidationIssue] = []
        nodes = tuple(plan.nodes)
        self._validate_plan_fields(
            plan,
            nodes,
            issues,
            validate_identity=validate_identity,
        )
        self._validate_exploration_plan_shape(plan, nodes, issues, executable=executable)

        node_index: dict[str, PlanNode] = {}
        for node in nodes:
            if node.node_id in node_index:
                issues.append(
                    _issue(
                        PlanValidationCode.DUPLICATE_NODE_ID,
                        f"node_id is duplicated: {node.node_id}",
                        node_id=node.node_id,
                        field="nodes",
                    )
                )
            else:
                node_index[node.node_id] = node
            self._validate_node(node, issues)

        adjacency = {node_id: set() for node_id in node_index}
        incoming = {node_id: 0 for node_id in node_index}
        valid_edges: list[tuple[int, str, str, ConditionExpr | None]] = []
        for edge_index, edge in enumerate(plan.edges):
            source_exists = edge.from_node in node_index
            target_exists = edge.to_node in node_index
            if not source_exists:
                issues.append(
                    _issue(
                        PlanValidationCode.EDGE_SOURCE_NOT_FOUND,
                        f"edge source does not exist: {edge.from_node}",
                        edge_index=edge_index,
                        field="from_node",
                        reference=edge.from_node,
                    )
                )
            if not target_exists:
                issues.append(
                    _issue(
                        PlanValidationCode.EDGE_TARGET_NOT_FOUND,
                        f"edge target does not exist: {edge.to_node}",
                        edge_index=edge_index,
                        field="to_node",
                        reference=edge.to_node,
                    )
                )
            if edge.from_node == edge.to_node:
                issues.append(
                    _issue(
                        PlanValidationCode.SELF_EDGE,
                        f"self edge is not allowed: {edge.from_node}",
                        edge_index=edge_index,
                        reference=edge.from_node,
                    )
                )
            if source_exists and target_exists and edge.from_node != edge.to_node:
                if edge.to_node not in adjacency[edge.from_node]:
                    adjacency[edge.from_node].add(edge.to_node)
                    incoming[edge.to_node] += 1
                valid_edges.append((edge_index, edge.from_node, edge.to_node, edge.condition))

        if nodes:
            roots = tuple(node_id for node_id, count in incoming.items() if count == 0)
            if not roots:
                issues.append(
                    _issue(
                        PlanValidationCode.NO_ROOT,
                        "plan must contain at least one root node",
                        field="nodes",
                    )
                )
            cycle_nodes = _find_cycle_remainder(adjacency, incoming)
            if cycle_nodes:
                issues.append(
                    _issue(
                        PlanValidationCode.CYCLE,
                        f"plan contains a cycle involving: {', '.join(cycle_nodes)}",
                        field="edges",
                        reference=",".join(cycle_nodes),
                    )
                )

        self._validate_bindings(plan, node_index, adjacency, issues)
        self._validate_outputs(plan, node_index, issues)
        self._validate_conditions(valid_edges, node_index, adjacency, issues)
        if executable and self._catalog is not None:
            self._validate_capabilities(nodes, issues)
        return tuple(issues)

    def _validate_plan_fields(
        self,
        plan: PlanShape,
        nodes: tuple[PlanNode, ...],
        issues: list[PlanValidationIssue],
        *,
        validate_identity: bool,
    ) -> None:
        if validate_identity:
            if not isinstance(plan, ExecutionPlan):
                raise TypeError("identity validation requires ExecutionPlan")
            if not isinstance(plan.plan_id, str) or not plan.plan_id.strip():
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_PLAN_ID,
                        "plan_id must be a non-empty string",
                        field="plan_id",
                    )
                )
            if (
                not isinstance(plan.revision, int)
                or isinstance(plan.revision, bool)
                or plan.revision < 1
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_REVISION,
                        "revision must be an integer greater than or equal to 1",
                        field="revision",
                    )
                )
        if not nodes:
            issues.append(
                _issue(
                    PlanValidationCode.EMPTY_PLAN,
                    "plan must contain at least one node",
                    field="nodes",
                )
            )
        deadline = plan.budget.deadline_at
        if deadline is not None and (deadline.tzinfo is None or deadline.utcoffset() is None):
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_DEADLINE,
                    "plan deadline must include timezone information",
                    field="budget.deadline_at",
                )
            )
        if plan.failure_policy is not FailurePolicy.FAIL_FAST:
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_FAILURE_POLICY,
                    "plan failure_policy must be fail_fast in stage two",
                    field="failure_policy",
                )
            )

    def _validate_node(
        self,
        node: PlanNode,
        issues: list[PlanValidationIssue],
    ) -> None:
        if node.kind is PlanNodeKind.CAPABILITY:
            if not isinstance(node.capability, str) or not node.capability.strip():
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CAPABILITY_NODE,
                        "capability node requires capability",
                        node_id=node.node_id,
                        field="capability",
                    )
                )
            if node.exploration is not None:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CAPABILITY_NODE,
                        "capability node forbids exploration spec",
                        node_id=node.node_id,
                        field="exploration",
                    )
                )
        elif node.kind is PlanNodeKind.APPROVAL:
            if (
                node.capability is not None
                or node.input_mapping
                or node.idempotency_key is not None
                or node.exploration is not None
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_APPROVAL_NODE,
                        "approval node forbids capability, input_mapping, idempotency_key, "
                        "and exploration spec",
                        node_id=node.node_id,
                    )
                )
        elif node.kind is PlanNodeKind.EXPLORATION:
            if (
                node.exploration is None
                or node.capability is not None
                or node.input_mapping
                or node.idempotency_key is not None
                or node.metadata
                or node.retry_policy.max_attempts != 1
            ):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_EXPLORATION_NODE,
                        "exploration node requires typed spec, no capability/input/idempotency/"
                        "metadata, and max_attempts=1",
                        node_id=node.node_id,
                    )
                )
        else:
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_NODE_KIND,
                    f"unsupported node kind: {node.kind}",
                    node_id=node.node_id,
                    field="kind",
                )
            )

        if node.timeout_ms is not None and (
            not isinstance(node.timeout_ms, int)
            or isinstance(node.timeout_ms, bool)
            or node.timeout_ms <= 0
        ):
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_TIMEOUT,
                    "node timeout_ms must be a positive integer",
                    node_id=node.node_id,
                    field="timeout_ms",
                )
            )
        if not _valid_retry_policy(node.retry_policy):
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_RETRY_POLICY,
                    "node retry_policy contains invalid attempts or backoff values",
                    node_id=node.node_id,
                    field="retry_policy",
                )
            )
        if node.failure_policy not in {FailurePolicy.FAIL_PLAN, FailurePolicy.CONTINUE}:
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_FAILURE_POLICY,
                    "node failure_policy must be fail_plan or continue",
                    node_id=node.node_id,
                    field="failure_policy",
                )
            )

    def _validate_bindings(
        self,
        plan: PlanShape,
        node_index: dict[str, PlanNode],
        adjacency: dict[str, set[str]],
        issues: list[PlanValidationIssue],
    ) -> None:
        request_roots = frozenset(Request.model_fields)
        result_roots = frozenset(ResultEnvelope.model_fields)
        for node in plan.nodes:
            bindings = (
                node.exploration.goal_bindings
                if node.kind is PlanNodeKind.EXPLORATION and node.exploration is not None
                else node.input_mapping
            )
            field_prefix = (
                "exploration.goal_bindings"
                if node.kind is PlanNodeKind.EXPLORATION
                else "input_mapping"
            )
            for input_name, binding in bindings.items():
                field = f"{field_prefix}.{input_name}"
                if not input_name.strip():
                    issues.append(
                        _issue(
                            PlanValidationCode.INVALID_BINDING,
                            "input mapping name must not be empty",
                            node_id=node.node_id,
                            field=field,
                        )
                    )
                if isinstance(binding, LiteralBinding):
                    continue
                if isinstance(binding, RequestBinding):
                    if not _pointer_has_known_root(binding.pointer, request_roots):
                        issues.append(
                            _issue(
                                PlanValidationCode.INVALID_BINDING,
                                f"request binding has invalid pointer: {binding.pointer}",
                                node_id=node.node_id,
                                field=field,
                                reference=binding.pointer,
                            )
                        )
                    continue
                if isinstance(binding, NodeOutputBinding):
                    if binding.node_id not in node_index:
                        issues.append(
                            _issue(
                                PlanValidationCode.INPUT_REFERENCE_NOT_FOUND,
                                f"input binding node does not exist: {binding.node_id}",
                                node_id=node.node_id,
                                field=field,
                                reference=binding.node_id,
                            )
                        )
                    elif not _is_ancestor(binding.node_id, node.node_id, adjacency):
                        issues.append(
                            _issue(
                                PlanValidationCode.INPUT_REFERENCE_UNAVAILABLE,
                                f"input source {binding.node_id} is not an ancestor "
                                f"of {node.node_id}",
                                node_id=node.node_id,
                                field=field,
                                reference=binding.node_id,
                            )
                        )
                    if not _pointer_has_known_root(binding.pointer, result_roots):
                        issues.append(
                            _issue(
                                PlanValidationCode.INVALID_BINDING,
                                f"node output binding has invalid pointer: {binding.pointer}",
                                node_id=node.node_id,
                                field=field,
                                reference=binding.pointer,
                            )
                        )
                    continue
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_BINDING,
                        f"unsupported input binding: {type(binding).__name__}",
                        node_id=node.node_id,
                        field=field,
                    )
                )

    def _validate_exploration_plan_shape(
        self,
        plan: PlanShape,
        nodes: tuple[PlanNode, ...],
        issues: list[PlanValidationIssue],
        *,
        executable: bool,
    ) -> None:
        exploration_nodes = tuple(node for node in nodes if node.kind is PlanNodeKind.EXPLORATION)
        if not exploration_nodes:
            return
        if executable:
            issues.append(
                _issue(
                    PlanValidationCode.EXPLORATION_NOT_AVAILABLE,
                    "exploration execution is not enabled until Foundation F4b",
                    field="nodes",
                )
            )
        valid_wrapper = (
            len(nodes) == 1
            and len(exploration_nodes) == 1
            and not plan.edges
            and len(plan.outputs) == 1
        )
        if valid_wrapper:
            node = exploration_nodes[0]
            output = next(iter(plan.outputs.values()))
            valid_wrapper = output.node_id == node.node_id and output.pointer == "/output"
        if not valid_wrapper:
            issues.append(
                _issue(
                    PlanValidationCode.INVALID_EXPLORATION_PLAN,
                    "minimal exploration must be a single-node, zero-edge wrapper whose "
                    "only output binds /output",
                    field="nodes",
                )
            )

    def _validate_outputs(
        self,
        plan: PlanShape,
        node_index: dict[str, PlanNode],
        issues: list[PlanValidationIssue],
    ) -> None:
        result_roots = frozenset(ResultEnvelope.model_fields)
        for output_name, binding in plan.outputs.items():
            field = f"outputs.{output_name}"
            if not output_name.strip():
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_OUTPUT,
                        "output mapping name must not be empty",
                        field=field,
                    )
                )
            if binding.node_id not in node_index:
                issues.append(
                    _issue(
                        PlanValidationCode.OUTPUT_REFERENCE_NOT_FOUND,
                        f"output binding node does not exist: {binding.node_id}",
                        field=field,
                        reference=binding.node_id,
                    )
                )
            if not _pointer_has_known_root(binding.pointer, result_roots):
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_OUTPUT,
                        f"output binding has invalid pointer: {binding.pointer}",
                        field=field,
                        reference=binding.pointer,
                    )
                )

    def _validate_conditions(
        self,
        edges: list[tuple[int, str, str, ConditionExpr | None]],
        node_index: dict[str, PlanNode],
        adjacency: dict[str, set[str]],
        issues: list[PlanValidationIssue],
    ) -> None:
        result_roots = frozenset(ResultEnvelope.model_fields)
        for edge_index, source, _, condition in edges:
            if condition is None:
                continue
            for expression in _condition_expressions(condition):
                if not _valid_condition_shape(expression):
                    issues.append(
                        _issue(
                            PlanValidationCode.INVALID_CONDITION,
                            f"condition has invalid {expression.operator} shape",
                            edge_index=edge_index,
                            field="condition",
                        )
                    )
                if expression.operator in {
                    ConditionOperator.LT,
                    ConditionOperator.LTE,
                    ConditionOperator.GT,
                    ConditionOperator.GTE,
                } and (
                    isinstance(expression.value, bool)
                    or not isinstance(expression.value, int | float | str)
                ):
                    issues.append(
                        _issue(
                            PlanValidationCode.INVALID_CONDITION,
                            f"{expression.operator.value} condition requires "
                            "an ordered scalar value",
                            edge_index=edge_index,
                            field="condition.value",
                        )
                    )
            for reference in _condition_references(condition):
                if reference.node_id not in node_index:
                    issues.append(
                        _issue(
                            PlanValidationCode.CONDITION_REFERENCE_NOT_FOUND,
                            f"condition node does not exist: {reference.node_id}",
                            edge_index=edge_index,
                            field="condition.ref",
                            reference=reference.node_id,
                        )
                    )
                elif reference.node_id != source and not _is_ancestor(
                    reference.node_id, source, adjacency
                ):
                    issues.append(
                        _issue(
                            PlanValidationCode.CONDITION_REFERENCE_UNAVAILABLE,
                            f"condition source {reference.node_id} is unavailable at {source}",
                            edge_index=edge_index,
                            field="condition.ref",
                            reference=reference.node_id,
                        )
                    )
                if not _pointer_has_known_root(reference.pointer, result_roots):
                    issues.append(
                        _issue(
                            PlanValidationCode.INVALID_CONDITION,
                            f"condition has invalid pointer: {reference.pointer}",
                            edge_index=edge_index,
                            field="condition.ref.pointer",
                            reference=reference.pointer,
                        )
                    )

    def _validate_capabilities(
        self,
        nodes: tuple[PlanNode, ...],
        issues: list[PlanValidationIssue],
    ) -> None:
        assert self._catalog is not None
        for node in nodes:
            if (
                node.kind is not PlanNodeKind.CAPABILITY
                or not isinstance(node.capability, str)
                or not node.capability.strip()
            ):
                continue
            descriptor = self._catalog.get(node.capability)
            if descriptor is None:
                issues.append(
                    _issue(
                        PlanValidationCode.CAPABILITY_NOT_FOUND,
                        f"capability is not available: {node.capability}",
                        node_id=node.node_id,
                        field="capability",
                        reference=node.capability,
                    )
                )
            elif descriptor.id != node.capability or descriptor.type not in {
                CapabilityType.AGENT,
                CapabilityType.TOOL,
            }:
                issues.append(
                    _issue(
                        PlanValidationCode.INVALID_CAPABILITY_DESCRIPTOR,
                        f"capability descriptor is inconsistent: {node.capability}",
                        node_id=node.node_id,
                        field="capability",
                        reference=node.capability,
                    )
                )


def _valid_retry_policy(policy: RetryPolicy) -> bool:
    return (
        isinstance(policy, RetryPolicy)
        and isinstance(policy.max_attempts, int)
        and not isinstance(policy.max_attempts, bool)
        and policy.max_attempts >= 1
        and isinstance(policy.initial_backoff_ms, int)
        and not isinstance(policy.initial_backoff_ms, bool)
        and policy.initial_backoff_ms >= 0
        and isinstance(policy.max_backoff_ms, int)
        and not isinstance(policy.max_backoff_ms, bool)
        and policy.max_backoff_ms >= policy.initial_backoff_ms
        and isinstance(policy.multiplier, int | float)
        and not isinstance(policy.multiplier, bool)
        and policy.multiplier >= 1
    )


def _find_cycle_remainder(
    adjacency: dict[str, set[str]],
    incoming: dict[str, int],
) -> tuple[str, ...]:
    remaining_incoming = dict(incoming)
    ready = deque(sorted(node_id for node_id, count in remaining_incoming.items() if count == 0))
    visited = 0
    while ready:
        node_id = ready.popleft()
        visited += 1
        for target in sorted(adjacency[node_id]):
            remaining_incoming[target] -= 1
            if remaining_incoming[target] == 0:
                ready.append(target)
    if visited == len(adjacency):
        return ()
    return tuple(sorted(node_id for node_id, count in remaining_incoming.items() if count > 0))


def _is_ancestor(source: str, target: str, adjacency: dict[str, set[str]]) -> bool:
    if source == target or source not in adjacency or target not in adjacency:
        return False
    pending = list(adjacency[source])
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency[current])
    return False


def _condition_references(condition: ConditionExpr) -> Iterable[ValueReference]:
    if condition.ref is not None:
        yield condition.ref
    for operand in condition.operands:
        yield from _condition_references(operand)


def _condition_expressions(condition: ConditionExpr) -> Iterable[ConditionExpr]:
    yield condition
    for operand in condition.operands:
        yield from _condition_expressions(operand)


def _valid_condition_shape(condition: ConditionExpr) -> bool:
    comparison_operators = {
        ConditionOperator.EQ,
        ConditionOperator.NE,
        ConditionOperator.LT,
        ConditionOperator.LTE,
        ConditionOperator.GT,
        ConditionOperator.GTE,
        ConditionOperator.EXISTS,
        ConditionOperator.IN,
    }
    if condition.operator in comparison_operators:
        if condition.ref is None or condition.operands:
            return False
        if condition.operator is ConditionOperator.EXISTS:
            return condition.value is None
        if condition.operator is ConditionOperator.IN:
            return isinstance(condition.value, tuple | list)
        return True
    if condition.operator in {ConditionOperator.AND, ConditionOperator.OR}:
        return condition.ref is None and condition.value is None and len(condition.operands) >= 2
    if condition.operator is ConditionOperator.NOT:
        return condition.ref is None and condition.value is None and len(condition.operands) == 1
    return False


def _pointer_has_known_root(pointer: str, roots: frozenset[str]) -> bool:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    if re.search(r"~(?:[^01]|$)", pointer):
        return False
    first_token = pointer[1:].split("/", 1)[0]
    decoded = first_token.replace("~1", "/").replace("~0", "~")
    return decoded in roots


def _issue(
    code: PlanValidationCode,
    message: str,
    *,
    node_id: str | None = None,
    edge_index: int | None = None,
    field: str | None = None,
    reference: str | None = None,
) -> PlanValidationIssue:
    return PlanValidationIssue(
        code=code,
        message=message,
        node_id=node_id,
        edge_index=edge_index,
        field=field,
        reference=reference,
    )
