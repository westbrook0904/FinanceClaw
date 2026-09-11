"""Retain native HITL payloads and persist rejection before the new root ReAct continues."""

import asyncio

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.agent_server.tools.subgraph_scope import active_scope, verify_scope
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.turns.types import ExecutionConflict


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
            if active_scope.get() is not None:
                context = verify_scope(self.execution, runtime.context).context
            else:
                context = ExecutionContext.model_validate(runtime.context)
                self.execution.verify_context(context)
                root = self.execution.get(context.turn_id)
                if not root["release_snapshot"]["profile"].get("worker_manifest"):
                    raise ExecutionConflict("native rejection requires the API root release")
            self.execution.deny_side_effects(context.turn_id)
        return result

    async def aafter_model(self, state, runtime):
        """Keep synchronous fact writes off the event loop; preserve native context variables."""
        return await asyncio.to_thread(self.after_model, state, runtime)
