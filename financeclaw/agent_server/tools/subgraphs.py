"""Precompiled Worker graphs exposed as governed root Agent Tools."""

import asyncio
import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from financeclaw.agent_server.tools.governance import ManagedTool
from financeclaw.agent_server.tools.subgraph_scope import (
    InvocationScope,
    active_scope,
    verify_scope,
)
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.context.references import resolve_context_refs
from financeclaw.shared.execution_ledger.authorization import intersect_scopes, require_scopes
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.releases.subgraphs import (
    composite_governance,
    composite_name,
    is_parallel_read_worker,
)


class SubagentInput(BaseModel):
    """Only bounded task data and authorized references are visible to the model."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    task: str = Field(min_length=1, max_length=8000)
    arguments: dict[str, Any] = Field(default_factory=dict)
    context_refs: tuple[str, ...] = Field(default=(), max_length=32)
    runtime: ToolRuntime[ExecutionContext]


class SubgraphTool(BaseTool):
    """Invoke one pinned compiled graph while retaining the root execution identity."""

    graph: Any
    release: Any
    declaration: str
    execution: Any
    conversations: Any = None
    artifacts: Any = None
    max_result_bytes: int = 16384

    def _run(self, **kwargs):
        """拒绝同步桥接，避免在已有事件循环中创建另一套调度。"""
        raise RuntimeError("internal subgraph Tools require asynchronous graph execution")

    def prepare(self, runtime, arguments):
        """Resolve explicit references under root ownership, then narrow Worker scopes."""
        context = ExecutionContext.model_validate(runtime.context)
        if not runtime.tool_call_id or active_scope.get() is not None:
            raise ExecutionConflict("only the root may invoke a published Worker Tool")
        if self.execution is None:
            raise ExecutionConflict("persistent root execution is required for subgraphs")
        self.execution.verify_context(context)
        root = self.execution.get(context.run_id)
        if self.declaration not in root["snapshot"].get("profile", {}).get("worker_manifest", []):
            raise ExecutionConflict("worker release is not pinned by this root")
        require_scopes(context.scopes, self.release.required_scopes)
        declaration = json.loads(self.declaration)
        needed = set(self.release.required_scopes)
        for leaf in declaration["tools"]:
            needed.update(leaf["required_scopes"])
        needed.update(
            point.required_scope
            for point in getattr(self.release, "interaction_points", ())
            if point.required_scope
        )
        # Explicit references may be read by the wrapper, before child scopes are narrowed.
        refs = arguments.get("context_refs", ())
        resolved = resolve_context_refs(
            tuple(refs),
            context=context,
            conversations=self.conversations,
            artifacts=self.artifacts,
        )
        worker_context = context.model_copy(
            update={"scopes": intersect_scopes(context.scopes, needed)}
        )
        scope = InvocationScope(
            self.declaration, worker_context, runtime.tool_call_id, digest(arguments)
        )
        return scope, resolved

    async def _arun(self, *, runtime: ToolRuntime[ExecutionContext], **arguments):
        """在原生父调用配置中等待子图；重入时复验发布和授权。"""
        arguments = self.args_schema.model_validate({**arguments, "runtime": runtime}).model_dump(
            mode="json", exclude={"runtime"}
        )
        scope, refs = await asyncio.to_thread(self.prepare, runtime, arguments)
        token = active_scope.set(scope)
        try:
            await asyncio.to_thread(verify_scope, self.execution, scope.context, self.declaration)
            value = self.worker_input(arguments, refs, runtime=runtime)
            try:
                result = await self.graph.ainvoke(
                    value, config=runtime.config, context=scope.context
                )
                public = self.public_result(result)
            except ValidationError as exc:
                raise ExecutionConflict(
                    "Worker state or public result violates its release"
                ) from exc
            # Native interrupts must bubble to the root ToolNode, never become ordinary results.
            if result.get("__interrupt__"):
                raise ExecutionConflict("nested graph returned an unpropagated interrupt")
            encoded = public.model_dump_json()
            if len(encoded.encode()) > self.max_result_bytes:
                raise ValueError("Worker public result exceeds the delivery budget")
            return encoded
        finally:
            active_scope.reset(token)


class SubagentTool(SubgraphTool):
    """Project task-only input and validated domain output for a specialist Agent."""

    args_schema: type[BaseModel] = SubagentInput

    def worker_input(self, arguments, refs, *, runtime):
        """传递当前用户原问题和显式授权引用，不复制整段根历史或模型推理。"""
        parsed = self.release.input_schema.model_validate(arguments.get("arguments", {}))
        user = next(
            (
                message
                for message in reversed(runtime.state.get("messages", []))
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        envelope = {
            "task": arguments["task"],
            "arguments": parsed.model_dump(mode="json"),
            "context_refs": refs,
            "user_context": {"message_id": user.id, "content": user.content} if user else None,
        }
        encoded = json.dumps(envelope, ensure_ascii=False)
        if len(encoded.encode()) > 48000:
            raise ValueError("Worker input exceeds the context budget")
        return {"messages": [HumanMessage(content=encoded)]}

    def public_result(self, result):
        """Validate only the declared public output; never expose internal Worker messages."""
        return self.release.output_schema.model_validate(result[self.release.output_state_key])


class WorkflowTool(SubgraphTool):
    """Execute a fixed Workflow as one exclusive Tool in the root ReAct loop."""

    def worker_input(self, arguments, refs, *, runtime):
        """Construct the declared Worker input without copying root messages or memory."""
        return self.release.normalize_input(arguments)

    def public_result(self, result):
        """Validate only the declared public output; never expose internal Worker messages."""
        return self.release.output_schema.model_validate(result)


def subgraph_tool(release, graph, declaration, *, execution, conversations=None, artifacts=None):
    """Bind the immutable release and native ToolRuntime injection at assembly time."""
    kind = json.loads(declaration)["kind"]
    options = {}
    if kind == "workflow":
        options["args_schema"] = type(
            f"{release.input_schema.__name__}SubgraphInput",
            (release.input_schema,),
            {
                "__annotations__": {"runtime": ToolRuntime[ExecutionContext]},
                "model_config": ConfigDict(extra="forbid", arbitrary_types_allowed=True),
            },
        )
    return ManagedTool(
        tool=(SubagentTool if kind == "agent" else WorkflowTool)(
            name=composite_name(release),
            release=release,
            graph=graph,
            declaration=declaration,
            execution=execution,
            conversations=conversations,
            artifacts=artifacts,
            description=(
                f"Run bounded {kind} {release.key[0]}@{release.version} inside this "
                "ReAct loop. Wait for its public result; human interrupts resume here. "
                + (release.description if kind == "agent" else "")
                + " "
                + (
                    "Parallel-safe read-only Worker: independent calls may share a batch. "
                    if is_parallel_read_worker(declaration)
                    else "Requires an exclusive Tool batch. "
                )
                + "If the result needs_clarification, ask the user and wait; "
                "never invent missing arguments. "
                "Input contract: "
                + json.dumps(release.input_schema.model_json_schema(), ensure_ascii=False)
            ),
            metadata={"preserve_result": True},
            **options,
        ),
        governance=composite_governance(release),
    )
