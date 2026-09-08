"""Retain native HITL payloads and persist a Worker rejection before ReAct continues."""

import asyncio

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.agent_server.tools.subgraph_scope import verify_scope


class SubgraphHITLMiddleware(HumanInTheLoopMiddleware):
    """Only approve/reject are enabled; a synthetic error ToolMessage records rejection."""

    def __init__(self, *, execution, **kwargs):
        """Bind the same root fact repository used by model and leaf budgets."""
        super().__init__(**kwargs)
        self.execution = execution

    def after_model(self, state, runtime):
        """Use native interrupt validation, then forbid new side effects after rejection."""
        result = super().after_model(state, runtime)
        if result and any(
            isinstance(message, ToolMessage) and message.status == "error"
            for message in result["messages"]
        ):
            scope = verify_scope(self.execution, runtime.context)
            self.execution.deny_side_effects(scope.context.run_id)
        return result

    async def aafter_model(self, state, runtime):
        """Keep synchronous fact writes off the event loop; preserve native context variables."""
        return await asyncio.to_thread(self.after_model, state, runtime)
