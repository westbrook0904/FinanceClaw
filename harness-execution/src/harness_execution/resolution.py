"""Basic Scheduler 使用的结构化 Binding 与 Condition 求值。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harness_contracts import (
    ConditionExpr,
    ConditionOperator,
    LiteralBinding,
    NodeOutputBinding,
    Request,
    RequestBinding,
    RequestInput,
    ResultEnvelope,
)


class JsonPointerResolutionError(ValueError):
    """JSON Pointer 无法在当前文档中解析。"""


class BindingResolutionError(ValueError):
    """节点输入 Binding 无法解析。"""


_MISSING = object()


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    """解析阶段二使用的 RFC 6901 风格 JSON Pointer。"""

    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise JsonPointerResolutionError("JSON pointer must start with '/'")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise JsonPointerResolutionError(f"object key not found: {token}")
            current = current[token]
            continue
        if isinstance(current, list | tuple):
            if not token.isdigit():
                raise JsonPointerResolutionError(f"array index is invalid: {token}")
            index = int(token)
            if index >= len(current):
                raise JsonPointerResolutionError(f"array index is out of range: {token}")
            current = current[index]
            continue
        raise JsonPointerResolutionError(f"cannot descend through scalar at token: {token}")
    return current


class InputResolver:
    """把 PlanNode.input_mapping 解析为标准 JSON RequestInput。"""

    def resolve(
        self,
        request: Request,
        input_mapping: Mapping[str, object],
        results: Mapping[str, ResultEnvelope],
    ) -> RequestInput:
        request_document = request.model_dump(mode="json")
        payload: dict[str, Any] = {}
        for input_name, binding in input_mapping.items():
            try:
                if isinstance(binding, LiteralBinding):
                    payload[input_name] = binding.model_dump(mode="json")["value"]
                elif isinstance(binding, RequestBinding):
                    payload[input_name] = resolve_json_pointer(
                        request_document,
                        binding.pointer,
                    )
                elif isinstance(binding, NodeOutputBinding):
                    result = results.get(binding.node_id)
                    if result is None:
                        raise JsonPointerResolutionError(
                            f"node result is unavailable: {binding.node_id}"
                        )
                    payload[input_name] = resolve_json_pointer(
                        result.model_dump(mode="json"),
                        binding.pointer,
                    )
                else:
                    raise JsonPointerResolutionError(
                        f"unsupported binding type: {type(binding).__name__}"
                    )
            except JsonPointerResolutionError as exc:
                raise BindingResolutionError(
                    f"failed to resolve input '{input_name}': {exc}"
                ) from exc
        return RequestInput(type="json", content=payload)


class ConditionEvaluator:
    """按白名单运算符递归计算 ConditionExpr，不执行任意表达式。"""

    def evaluate(
        self,
        condition: ConditionExpr,
        results: Mapping[str, ResultEnvelope],
    ) -> bool:
        operator = condition.operator
        if operator is ConditionOperator.AND:
            return all(self.evaluate(item, results) for item in condition.operands)
        if operator is ConditionOperator.OR:
            return any(self.evaluate(item, results) for item in condition.operands)
        if operator is ConditionOperator.NOT:
            return not self.evaluate(condition.operands[0], results)

        assert condition.ref is not None
        actual = self._resolve_reference(
            condition.ref.node_id,
            condition.ref.pointer,
            results,
        )
        if operator is ConditionOperator.EXISTS:
            return actual is not _MISSING
        if actual is _MISSING:
            return False

        expected = condition.model_dump(mode="json")["value"]
        try:
            if operator is ConditionOperator.EQ:
                return actual == expected
            if operator is ConditionOperator.NE:
                return actual != expected
            if operator is ConditionOperator.LT:
                return actual < expected
            if operator is ConditionOperator.LTE:
                return actual <= expected
            if operator is ConditionOperator.GT:
                return actual > expected
            if operator is ConditionOperator.GTE:
                return actual >= expected
            if operator is ConditionOperator.IN:
                return actual in expected
        except TypeError:
            return False
        raise ValueError(f"unsupported condition operator: {operator}")

    def _resolve_reference(
        self,
        node_id: str,
        pointer: str,
        results: Mapping[str, ResultEnvelope],
    ) -> Any:
        result = results.get(node_id)
        if result is None:
            return _MISSING
        try:
            return resolve_json_pointer(result.model_dump(mode="json"), pointer)
        except JsonPointerResolutionError:
            return _MISSING
