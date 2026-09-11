"""Native LangGraph commands and checkpoint evidence through the injected SDK client."""

from langgraph_sdk.errors import NotFoundError

from financeclaw.kernel.turns import current_turn_start, is_user_message
from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.turns.budget import snapshot_context
from financeclaw.shared.turns.types import ExecutionConflict, digest


def interrupts(state):
    """Normalize native top-level or task-level interrupt evidence."""
    return list(state.get("interrupts") or []) or [
        item for task in state.get("tasks", []) for item in task.get("interrupts", [])
    ]


def current_messages(state, snapshot):
    """Isolate messages after this Turn input and reject evidence from a later task."""
    messages = state.get("values", {}).get("messages", [])
    try:
        start = current_turn_start(messages, snapshot["user_message_id"])
    except ValueError as exc:
        raise ExecutionConflict(str(exc)) from exc
    current = messages[start + 1 :]
    if any(is_user_message(item) for item in current):
        raise ExecutionConflict("native thread advanced to another Turn")
    return current


def final_text(message):
    """Extract public text from an assistant message without returning native state."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, (str, dict))
        )
    return ""


class NativeRuns:
    """One SDK transport. Production injects get_client(url=None, api_key=None)."""

    def __init__(self, client, releases, execution):
        """Inject dependencies without starting background work."""
        self.client, self.releases, self.execution = client, releases, execution

    @staticmethod
    def verify(run, turn, command):
        """Validate the native receipt against the frozen Turn and current command metadata."""
        metadata = run.get("metadata", {})
        if (
            run.get("thread_id") != turn["thread_id"]
            or metadata.get("turn_id") != turn["turn_id"]
            or metadata.get("command_id") != command["command_id"]
            or metadata.get("request_hash") != command["request_hash"]
            or metadata.get("release_hash") != turn["release_hash"]
        ):
            raise ExecutionConflict("native receipt differs from accepted command")
        if command.get("native_run_id") and run.get("run_id") != command["native_run_id"]:
            raise ExecutionConflict("native receipt changed")
        return run

    async def get(self, turn, command):
        """Fetch and verify the exact native run attached to the accepted command."""
        return self.verify(
            await self.client.runs.get(turn["thread_id"], command["native_run_id"]), turn, command
        )

    async def lookup(self, turn, command):
        """Exhaust pagination. Zero results never grants permission to send again."""
        matches, offset = [], 0
        while True:
            try:
                page = await self.client.runs.list(turn["thread_id"], limit=100, offset=offset)
            except NotFoundError:
                page = []
            for run in page:
                if run.get("metadata", {}).get("command_id") == command["command_id"]:
                    matches.append(self.verify(run, turn, command))
            if len(page) < 100:
                break
            offset += len(page)
        if len(matches) > 1:
            raise ExecutionConflict("multiple native receipts for one command")
        return matches[0]["run_id"] if matches else None

    async def validate_resume(self, turn, command):
        """Verify that the answered interrupt still belongs to its exact parent checkpoint."""
        payload = command["request_payload"]
        binding = payload["binding"]
        predecessor = await self.client.runs.get(turn["thread_id"], binding["native_run_id"])
        if (
            predecessor.get("metadata", {}).get("turn_id") != turn["turn_id"]
            or predecessor.get("metadata", {}).get("command_id") != payload["predecessor"]
        ):
            raise ExecutionConflict("resume predecessor does not belong to this Turn")
        state = await self.checkpoint_state(turn["thread_id"])
        waits = interrupts(state)
        if (
            state.get("checkpoint") != binding["checkpoint"]
            or state.get("metadata", {}).get("run_id") != binding["anchor_run_id"]
            or len(waits) != 1
            or waits[0].get("id") != binding["interrupt_id"]
            or digest(waits[0].get("value")) != binding["interrupt_hash"]
        ):
            raise ExecutionConflict("native interaction moved beyond the accepted answer")
        current_messages(state, turn["release_snapshot"])

    async def submit(self, turn, command):
        """Issue exactly one previously claimed start/resume outside a SQL transaction."""
        snapshot = turn["release_snapshot"]
        if (
            command["state"] != "sending"
            or digest(command["request_payload"]) != command["request_hash"]
        ):
            raise ExecutionConflict("command has no immutable sending right")
        self.releases.verify(snapshot)
        context = snapshot_context(
            snapshot, command["authorized_scopes"], command_id=command["command_id"]
        )
        await run_sync(self.execution.verify_context, context)
        payload = command["request_payload"]
        if command["kind"] == "start":
            arguments = {"input": payload["input"], "if_not_exists": "create"}
        else:
            await self.validate_resume(turn, command)
            arguments = {
                "command": {"resume": {payload["binding"]["interrupt_id"]: payload["response"]}},
                "checkpoint": payload["binding"]["checkpoint"],
                "if_not_exists": "reject",
            }
        run = await self.client.runs.create(
            turn["thread_id"],
            snapshot["assistant_id"],
            **arguments,
            context=context.model_dump(mode="json"),
            metadata={
                **context.trace_metadata(),
                "command_id": command["command_id"],
                "request_hash": command["request_hash"],
                "release_hash": turn["release_hash"],
            },
            multitask_strategy="reject",
            durability="sync",
        )
        return self.verify(run, turn, command)["run_id"]

    async def join(self, turn, command):
        """Wait for a native terminal hint without leasing or executing graph work."""
        await self.client.runs.join(turn["thread_id"], command["native_run_id"])

    async def cancel(self, turn, command):
        """Persist cancellation intent; observation confirms when execution has stopped."""
        await self.get(turn, command)
        await self.client.runs.cancel(turn["thread_id"], command["native_run_id"], wait=True)

    async def checkpoint_state(self, thread_id):
        """Pin the latest address, then read immutable checkpoint evidence with nested tasks."""
        latest = dict(await self.client.threads.get_state(thread_id, subgraphs=True))
        checkpoint = latest.get("checkpoint")
        if not checkpoint or not checkpoint.get("checkpoint_id"):
            raise ExecutionConflict("native state has no addressable checkpoint")
        state = dict(
            await self.client.threads.get_state(thread_id, checkpoint=checkpoint, subgraphs=True)
        )
        if state.get("checkpoint") != checkpoint:
            raise ExecutionConflict("native checkpoint identity changed during observation")
        return state

    async def observe(self, turn, command):
        """Native success alone cannot prove completion: interrupts may still be present."""
        native = await self.get(turn, command)
        status = native["status"]
        if status in {"pending", "running"}:
            return {"status": "queued" if status == "pending" else "running"}
        if status in {"error", "timeout"}:
            return {"status": "failed", "reason": "native_" + status}
        if status not in {"success", "interrupted"}:
            raise ExecutionConflict("unsupported native run status")
        state = await self.checkpoint_state(turn["thread_id"])
        native_run_id = native["run_id"]
        anchor = state.get("metadata", {}).get("run_id")
        waiting = interrupts(state)
        if any(task.get("error") for task in state.get("tasks", [])):
            return {"status": "failed", "reason": "native_task_error"}
        if not state.get("checkpoint", {}).get("checkpoint_id"):
            raise ExecutionConflict("native state has no addressable parent checkpoint")
        current_messages(state, turn["release_snapshot"])
        if waiting:
            if len(waiting) != 1 or not waiting[0].get("id"):
                raise ExecutionConflict("only one native human wait is supported")
            if anchor != native_run_id:
                binding = command["request_payload"].get("binding", {})
                if (
                    command["kind"] != "resume"
                    or state["checkpoint"] != binding.get("checkpoint")
                    or anchor != binding.get("anchor_run_id")
                    or waiting[0]["id"] == binding.get("interrupt_id")
                ):
                    raise ExecutionConflict("waiting checkpoint is not linked to this attempt")
            return {"status": "waiting", "state": state, "native_run_id": native_run_id}
        if status != "success" or anchor != native_run_id or state.get("next"):
            raise ExecutionConflict("final checkpoint does not prove active attempt completion")
        messages = current_messages(state, turn["release_snapshot"])
        if not messages or messages[-1].get("type", messages[-1].get("role")) not in {
            "ai",
            "assistant",
        }:
            raise ExecutionConflict("completed Turn has no final assistant message")
        final = messages[-1]
        if final.get("tool_calls") or final.get("additional_kwargs", {}).get("tool_calls"):
            raise ExecutionConflict("assistant still has pending Tool calls")
        content = final_text(final)
        if not content:
            raise ExecutionConflict("completed Turn has no public final text")
        return {
            "status": "completed",
            "content": content,
            "checkpoint": state["checkpoint"],
            "native_run_id": native_run_id,
        }
