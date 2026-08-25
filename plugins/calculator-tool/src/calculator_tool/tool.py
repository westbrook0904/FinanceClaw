"""阶段一确定性计算 Tool。"""

from __future__ import annotations

from typing import TypeAlias

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityType,
    InvocationContext,
    ResultEnvelope,
    ResultOutput,
)
from harness_spi import ToolRequest, ToolSPI

Number: TypeAlias = int | float


class CalculatorTool(ToolSPI):
    """执行四则运算，用于验证确定性 Tool 调用链。"""

    _descriptor = CapabilityDescriptor(
        id="math.calculate/v1",
        name="Calculator",
        type=CapabilityType.TOOL,
        version="1.0.0",
        input_schema={
            "type": "object",
            "required": ["operation", "left", "right"],
            "properties": {
                "operation": {"enum": ["add", "subtract", "multiply", "divide"]},
                "left": {"type": "number"},
                "right": {"type": "number"},
            },
        },
        output_schema={"type": "number"},
        tags=frozenset({"example", "local", "math"}),
        metadata={"deterministic": True},
    )

    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def execute(
        self,
        request: ToolRequest,
        context: InvocationContext,
    ) -> ResultEnvelope:
        arguments = request.model_dump(mode="json")["arguments"]
        operation = arguments.get("operation")
        left = self._require_number(arguments.get("left"), name="left")
        right = self._require_number(arguments.get("right"), name="right")

        if operation == "add":
            value = left + right
        elif operation == "subtract":
            value = left - right
        elif operation == "multiply":
            value = left * right
        elif operation == "divide":
            if right == 0:
                raise CapabilityError(
                    "calculator cannot divide by zero",
                    code="PLUGIN.CALCULATOR.DIVISION_BY_ZERO",
                )
            value = left / right
        else:
            raise CapabilityError(
                "unsupported calculator operation",
                code="PLUGIN.CALCULATOR.INVALID_OPERATION",
                details={"operation": str(operation)},
            )

        return ResultEnvelope.success(
            ResultOutput(type="number", data=value),
            metadata={
                "capability_id": self._descriptor.id,
                "operation": str(operation),
                "request_id": context.request.request_id,
            },
        )

    @staticmethod
    def _require_number(value: object, *, name: str) -> Number:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CapabilityError(
                f"calculator argument must be a number: {name}",
                code="PLUGIN.CALCULATOR.INVALID_ARGUMENT",
                details={"argument": name},
            )
        return value
