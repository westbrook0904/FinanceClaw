"""harness-spi 的接口与边界测试。"""

from __future__ import annotations

import inspect
import unittest

from harness_contracts import (
    CapabilityDescriptor,
    CapabilityType,
    InvocationContext,
    Request,
    RequestInput,
    RequestTarget,
    ResultEnvelope,
    ResultOutput,
)
from harness_spi import (
    AgentRequest,
    AgentSPI,
    PluginManifest,
    PluginSPI,
    ToolRequest,
    ToolSPI,
    validate_manifest_capabilities,
)
from pydantic import ValidationError


def make_context(capability: str) -> InvocationContext:
    return InvocationContext(
        request=Request(
            input=RequestInput(type="json", content={}),
            target=RequestTarget(capability=capability),
        )
    )


class EchoAgent(AgentSPI):
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id="echo.reply/v1",
            name="Echo Reply",
            type=CapabilityType.AGENT,
            version="1.0.0",
        )

    async def invoke(self, request: AgentRequest, context: InvocationContext) -> ResultEnvelope:
        return ResultEnvelope.success(
            ResultOutput(type=request.input.type, data=request.input.content)
        )


class CalculatorTool(ToolSPI):
    def descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id="math.calculate/v1",
            name="Calculator",
            type=CapabilityType.TOOL,
            version="1.0.0",
        )

    async def execute(self, request: ToolRequest, context: InvocationContext) -> ResultEnvelope:
        return ResultEnvelope.success(ResultOutput(type="number", data=3))


class ExamplePlugin(PluginSPI):
    def __init__(self) -> None:
        self.agent = EchoAgent()
        self.tool = CalculatorTool()

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="example-plugin",
            name="Example Plugin",
            version="1.0.0",
            sdk_version="1",
            capabilities=("echo.reply/v1", "math.calculate/v1"),
        )

    def capabilities(self) -> tuple[AgentSPI | ToolSPI, ...]:
        return (self.agent, self.tool)

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class SPITests(unittest.IsolatedAsyncioTestCase):
    def test_interfaces_are_abstract_and_keep_execution_semantics_separate(self) -> None:
        self.assertTrue(inspect.isabstract(AgentSPI))
        self.assertTrue(inspect.isabstract(ToolSPI))
        self.assertTrue(inspect.isabstract(PluginSPI))
        self.assertFalse(hasattr(PluginSPI, "execute"))
        self.assertFalse(hasattr(ToolSPI, "invoke"))

    async def test_agent_and_tool_return_shared_result_envelope(self) -> None:
        agent_result = await EchoAgent().invoke(
            AgentRequest(input=RequestInput(type="text", content="hello")),
            make_context("echo.reply/v1"),
        )
        tool_result = await CalculatorTool().execute(
            ToolRequest(arguments={"left": 1, "right": 2}),
            make_context("math.calculate/v1"),
        )

        self.assertEqual(agent_result.output.data, "hello")
        self.assertEqual(tool_result.output.data, 3)

    def test_manifest_requires_unique_non_empty_capabilities(self) -> None:
        with self.assertRaises(ValidationError):
            PluginManifest(
                plugin_id="invalid",
                name="Invalid",
                version="1.0.0",
                sdk_version="1",
                capabilities=(),
            )

    def test_spi_payloads_are_deeply_immutable(self) -> None:
        request = ToolRequest(arguments={"nested": {"items": [1]}})
        manifest = PluginManifest(
            plugin_id="immutable",
            name="Immutable",
            version="1.0.0",
            sdk_version="1",
            capabilities=("immutable.test/v1",),
            metadata={"nested": {"enabled": True}},
        )

        with self.assertRaises(TypeError):
            request.arguments["new"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            request.arguments["nested"]["items"] = []  # type: ignore[index]
        with self.assertRaises(TypeError):
            manifest.metadata["new"] = True  # type: ignore[index]

        self.assertEqual(request.model_dump(mode="json")["arguments"]["nested"]["items"], [1])

    def test_manifest_and_provider_descriptors_must_match(self) -> None:
        plugin = ExamplePlugin()
        descriptors = tuple(provider.descriptor() for provider in plugin.capabilities())
        validate_manifest_capabilities(plugin.manifest(), descriptors)

        with self.assertRaises(ValueError):
            validate_manifest_capabilities(plugin.manifest(), descriptors[:1])


if __name__ == "__main__":
    unittest.main()
