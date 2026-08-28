"""阶段一示例业务插件的行为与 Harness 集成测试。"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from calculator_tool import CalculatorTool, CalculatorToolPlugin
from echo_agent import EchoAgent, EchoAgentPlugin
from harness_bootstrap import build_harness
from harness_contracts import (
    CapabilityError,
    InvocationContext,
    Request,
    RequestInput,
    RequestTarget,
    ResultStatus,
)
from harness_spi import AgentRequest, ToolRequest
from mock_finance_agent import MockFinanceAgent, MockFinanceAgentPlugin

ROOT = Path(__file__).resolve().parents[2]


def make_context(capability_id: str, content: object) -> InvocationContext:
    return InvocationContext(
        request=Request(
            input=RequestInput(type="json", content=content),
            target=RequestTarget(capability=capability_id),
        )
    )


class EchoAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_echo_agent_preserves_input_type_and_content(self) -> None:
        agent = EchoAgent()
        context = make_context("echo.reply/v1", {"message": "hello"})

        result = await agent.invoke(
            AgentRequest(input=RequestInput(type="json", content={"message": "hello"})),
            context,
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertEqual(result.output.type, "json")
        self.assertEqual(result.output.data["message"], "hello")
        self.assertEqual(result.metadata["capability_id"], "echo.reply/v1")


class CalculatorToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_calculator_supports_four_basic_operations(self) -> None:
        tool = CalculatorTool()
        cases = (
            ("add", 7, 5, 12),
            ("subtract", 7, 5, 2),
            ("multiply", 7, 5, 35),
            ("divide", 7, 2, 3.5),
        )

        for operation, left, right, expected in cases:
            with self.subTest(operation=operation):
                context = make_context("math.calculate/v1", {})
                result = await tool.execute(
                    ToolRequest(
                        arguments={
                            "operation": operation,
                            "left": left,
                            "right": right,
                        }
                    ),
                    context,
                )
                self.assertEqual(result.output.data, expected)

    async def test_calculator_rejects_invalid_operation(self) -> None:
        tool = CalculatorTool()
        context = make_context("math.calculate/v1", {})

        with self.assertRaises(CapabilityError) as raised:
            await tool.execute(
                ToolRequest(arguments={"operation": "sqrt", "left": 4, "right": 0}),
                context,
            )

        self.assertEqual(raised.exception.code, "PLUGIN.CALCULATOR.INVALID_OPERATION")

    async def test_calculator_rejects_boolean_as_number(self) -> None:
        tool = CalculatorTool()
        context = make_context("math.calculate/v1", {})

        with self.assertRaises(CapabilityError) as raised:
            await tool.execute(
                ToolRequest(arguments={"operation": "add", "left": True, "right": 1}),
                context,
            )

        self.assertEqual(raised.exception.code, "PLUGIN.CALCULATOR.INVALID_ARGUMENT")

    async def test_calculator_rejects_division_by_zero(self) -> None:
        tool = CalculatorTool()
        context = make_context("math.calculate/v1", {})

        with self.assertRaises(CapabilityError) as raised:
            await tool.execute(
                ToolRequest(arguments={"operation": "divide", "left": 1, "right": 0}),
                context,
            )

        self.assertEqual(raised.exception.code, "PLUGIN.CALCULATOR.DIVISION_BY_ZERO")


class MockFinanceAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_finance_agent_returns_explicit_mock_payload(self) -> None:
        agent = MockFinanceAgent()
        context = make_context("finance.mock-query/v1", "revenue trend")

        result = await agent.invoke(
            AgentRequest(input=RequestInput(type="text", content="revenue trend")),
            context,
        )

        self.assertEqual(result.status, ResultStatus.SUCCESS)
        self.assertTrue(result.output.data["mock"])
        self.assertEqual(result.output.data["input"]["content"], "revenue trend")
        self.assertEqual(result.metadata["data_source"], "none")


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_lifecycle_is_idempotent_and_capabilities_are_stable(self) -> None:
        plugins = (EchoAgentPlugin(), CalculatorToolPlugin(), MockFinanceAgentPlugin())

        for plugin in plugins:
            with self.subTest(plugin=plugin.manifest().plugin_id):
                first = plugin.capabilities()
                await plugin.initialize()
                await plugin.initialize()
                second = plugin.capabilities()
                self.assertTrue(plugin.initialized)
                self.assertIs(first[0], second[0])
                await plugin.shutdown()
                await plugin.shutdown()
                self.assertFalse(plugin.initialized)

    def test_manifests_match_provider_descriptors(self) -> None:
        plugins = (EchoAgentPlugin(), CalculatorToolPlugin(), MockFinanceAgentPlugin())

        for plugin in plugins:
            with self.subTest(plugin=plugin.manifest().plugin_id):
                ids = tuple(provider.descriptor().id for provider in plugin.capabilities())
                self.assertEqual(plugin.manifest().capabilities, ids)


class PluginBootstrapIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_example_plugins_work_through_bootstrap_and_runtime(self) -> None:
        plugins = (EchoAgentPlugin(), CalculatorToolPlugin(), MockFinanceAgentPlugin())
        app = build_harness(plugins=plugins, entry_point_group=None)

        async with app:
            capability_ids = tuple(item.descriptor.id for item in app.registry.list())
            self.assertEqual(
                capability_ids,
                ("echo.reply/v1", "finance.mock-query/v1", "math.calculate/v1"),
            )

            echo = await app.invoke(
                Request(
                    input=RequestInput(type="text", content="hello"),
                    target=RequestTarget(capability="echo.reply/v1"),
                )
            )
            calculate = await app.invoke(
                Request(
                    input=RequestInput(
                        type="json",
                        content={"operation": "multiply", "left": 6, "right": 7},
                    ),
                    target=RequestTarget(capability="math.calculate/v1"),
                )
            )
            finance = await app.invoke(
                Request(
                    input=RequestInput(type="text", content="mock revenue"),
                    target=RequestTarget(capability="finance.mock-query/v1"),
                )
            )

            self.assertEqual(echo.output.data, "hello")
            self.assertEqual(calculate.output.data, 42)
            self.assertTrue(finance.output.data["mock"])
            self.assertIsNotNone(echo.trace_id)
            self.assertIsNotNone(calculate.trace_id)
            self.assertIsNotNone(finance.trace_id)

        self.assertEqual(app.registry.list(), ())

    async def test_plugin_error_is_normalized_by_runtime(self) -> None:
        app = build_harness(
            plugins=(CalculatorToolPlugin(),),
            entry_point_group=None,
        )

        async with app:
            result = await app.invoke(
                Request(
                    input=RequestInput(
                        type="json",
                        content={"operation": "divide", "left": 10, "right": 0},
                    ),
                    target=RequestTarget(capability="math.calculate/v1"),
                )
            )

        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.error.code, "PLUGIN.CALCULATOR.DIVISION_BY_ZERO")


class PluginPackagingTests(unittest.TestCase):
    def test_pyproject_registers_all_plugins_as_entry_points(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)

        entry_points = config["project"]["entry-points"]["financeclaw.plugins"]
        self.assertEqual(
            entry_points,
            {
                "calculator-tool": "calculator_tool:CalculatorToolPlugin",
                "echo-agent": "echo_agent:EchoAgentPlugin",
                "mock-finance-agent": "mock_finance_agent:MockFinanceAgentPlugin",
            },
        )


if __name__ == "__main__":
    unittest.main()
