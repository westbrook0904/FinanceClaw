"""Trusted in-process invocation scope; never accepted from model or HTTP context."""

import json
from contextvars import ContextVar
from dataclasses import dataclass

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.turns.snapshots import verify_agent_snapshot
from financeclaw.shared.turns.types import ExecutionConflict, digest


@dataclass(frozen=True, slots=True)
class InvocationScope:
    """Server-owned binding recreated when the outer Tool re-enters after resume."""

    declaration: str
    context: ExecutionContext
    tool_call_id: str
    arguments_hash: str

    @property
    def identity(self):
        """Separate repeated calls even when the Worker and business arguments are identical."""
        return digest(
            [self.context.turn_id, self.tool_call_id, self.declaration, self.arguments_hash]
        )

    def interaction_binding(self):
        """Expose bounded correlation fields; the native interrupt ID identifies the wait."""
        release = json.loads(self.declaration)
        return {
            "turn_id": self.context.turn_id,
            "root_tool_call_id": self.tool_call_id,
            "worker_kind": release["kind"],
            "worker_id": release["target_id"],
            "worker_version": release["version"],
            "invocation_id": self.identity,
        }


active_scope: ContextVar[InvocationScope | None] = ContextVar(
    "financeclaw_subgraph_scope", default=None
)


def verify_scope(repository, context, declaration=None):
    """Verify the live grant, root identity and pinned Worker before nested execution."""
    scope = active_scope.get()
    if scope is None or scope.context != context:
        raise ExecutionConflict("worker requires a trusted internal invocation scope")
    if declaration is not None and scope.declaration != declaration:
        raise ExecutionConflict("worker release differs from the bound invocation")
    if context.conversation_id is None or context.command_id is None:
        raise ExecutionConflict("subgraph must execute inside the business root")
    if repository is None:
        raise ExecutionConflict("persistent root execution is required for subgraphs")
    repository.verify_context(context)
    root = repository.get(context.turn_id)
    if scope.declaration not in root["release_snapshot"].get("profile", {}).get(
        "worker_manifest", []
    ):
        raise ExecutionConflict("worker release is not pinned by this root")
    return scope


def verify_graph_release(repository, context, profile):
    """Compare Workers to the manifest and root graphs to their own snapshot."""
    if profile.context_policy == "worker-task-only-v1":
        scope = verify_scope(repository, context)
        if json.loads(scope.declaration)["profile"] != profile.model_dump(mode="json"):
            raise ExecutionConflict("executing worker differs from the pinned profile")
    else:
        repository.verify_context(context)
        verify_agent_snapshot(profile, repository.get(context.turn_id)["release_snapshot"])
