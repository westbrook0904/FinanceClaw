"""Exact native root attempts; read observation is separate from command authority."""

import json

from langgraph_sdk.errors import NotFoundError

from financeclaw.kernel.backend import BackendExecutionRef
from financeclaw.kernel.turns import current_turn_start, is_user_message
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)


def native_id(thread_id, run_id):
    """Encode the exact native attempt in the shared receipt index."""
    return json.dumps([thread_id, str(run_id)], separators=(",", ":"))


def interrupts(state):
    """Native child waits propagate to the root even when nested task.state is invisible."""
    return list(state.get("interrupts") or []) or [
        item for task in state.get("tasks", []) for item in task.get("interrupts", [])
    ]


def current_messages(state, snapshot):
    """Exclude previous Turns using the BFF's immutable user Journal message ID."""
    messages = state.get("values", {}).get("messages", [])
    try:
        start = current_turn_start(messages, snapshot["user_message_id"])
    except ValueError as exc:
        raise ExecutionConflict(str(exc)) from exc
    current = messages[start + 1 :]
    if any(is_user_message(item) for item in current):
        raise ExecutionConflict("native thread advanced to another Turn")
    return current


class NativeRunReader:
    """Read-only backend capability: lookup exact receipts and checkpoint evidence."""

    def __init__(self, native, store):
        """Inject an authenticated transport and immutable application facts."""
        self.native, self.store = native, store
        self.client = native._client

    def reference(self, operation, snapshot, run_id):
        """Create a bounded exact receipt; backend identity comes only from deployment config."""
        return BackendExecutionRef(
            backend_instance_id=self.store.backend_instance_id,
            task_id=operation["run_id"],
            operation_id=operation["operation_id"],
            execution_id=native_id(snapshot["thread_id"], run_id),
        )

    async def lookup(self, operation, snapshot):
        """Keep empty lookups uncertain without granting permission to resend."""
        try:
            found = await self.native.find_operation(
                thread_id=snapshot["thread_id"], operation_id=operation["operation_id"]
            )
        except NotFoundError:
            found = None
        if found is None:
            return None
        reference = self.reference(operation, snapshot, found.run_id)
        await self.get_run(reference)
        return reference

    async def get_run(self, reference):
        """Verify operation metadata rather than accepting a thread's latest receipt."""
        thread_id, run_id = json.loads(reference.execution_id)
        native = await self.client.runs.get(thread_id, run_id)
        if (
            native.get("run_id") != run_id
            or native.get("thread_id") != thread_id
            or native.get("metadata", {}).get("operation_id") != reference.operation_id
            or native.get("metadata", {}).get("application_run_id") != reference.task_id
        ):
            raise ExecutionConflict("native receipt differs from fixed BFF operation")
        return native

    async def observe(self, reference, operation, snapshot):
        """Prove a current wait or final answer using the active attempt and parent anchor."""
        native = await self.get_run(reference)
        status = native["status"]
        if status in {"pending", "running"}:
            return {"status": "running"}
        if status in {"error", "timeout"}:
            return {"status": "failed", "reason": "backend_" + status}
        if status not in {"success", "interrupted"}:
            raise ExecutionConflict("unsupported native run status")
        state = dict(await self.client.threads.get_state(snapshot["thread_id"], subgraphs=True))
        run_id = native["run_id"]
        anchor = state.get("metadata", {}).get("run_id")
        waiting = interrupts(state)
        if any(task.get("error") for task in state.get("tasks", [])):
            return {"status": "failed", "reason": "native_task_error"}
        if not state.get("checkpoint", {}).get("checkpoint_id"):
            raise ExecutionConflict("native state has no addressable parent checkpoint")
        current_messages(state, snapshot)
        if waiting:
            if len(waiting) != 1 or not waiting[0].get("id"):
                raise ExecutionConflict("only one native human wait is supported")
            if anchor != run_id:
                # HF-0: repeated child questions can leave the parent anchor unchanged.
                payload = operation["request"]["payload"]
                binding = payload.get("binding", {})
                if (
                    operation["request"]["kind"] != "resume"
                    or state["checkpoint"] != binding.get("checkpoint")
                    or anchor != binding.get("anchor_run_id")
                    or waiting[0]["id"] == binding.get("interrupt_id")
                ):
                    raise ExecutionConflict("waiting checkpoint is not linked to this attempt")
            return {"status": "waiting", "state": state, "native_run_id": run_id}
        if status != "success" or anchor != run_id or state.get("next"):
            raise ExecutionConflict("final checkpoint does not prove active attempt completion")
        messages = current_messages(state, snapshot)
        if not messages or messages[-1].get("type", messages[-1].get("role")) not in {
            "ai",
            "assistant",
        }:
            raise ExecutionConflict("completed root has no final assistant message")
        final = messages[-1]
        if final.get("tool_calls") or final.get("additional_kwargs", {}).get("tool_calls"):
            raise ExecutionConflict("assistant still has pending Tool calls")
        from financeclaw.shared.backends.streaming import final_assistant_content

        content = final_assistant_content({"messages": [final]})
        if not content:
            raise ExecutionConflict("completed root has no public final text")
        return {
            "status": "completed",
            "content": content,
            "checkpoint": state["checkpoint"],
            "native_run_id": run_id,
        }

    async def validate_resume(self, operation, snapshot):
        """Check the exact still-pending native wait before submitting the frozen answer."""
        payload = operation["request"]["payload"]
        binding = payload["binding"]
        predecessor = self.store.execution.operation(operation["request"]["predecessor"])
        reference = self.reference(predecessor, snapshot, binding["native_run_id"])
        await self.get_run(reference)
        state = dict(await self.client.threads.get_state(snapshot["thread_id"], subgraphs=True))
        waits = interrupts(state)
        if (
            state.get("checkpoint") != binding["checkpoint"]
            or state.get("metadata", {}).get("run_id") != binding["anchor_run_id"]
            or len(waits) != 1
            or waits[0].get("id") != binding["interrupt_id"]
            or digest(waits[0].get("value")) != binding["interrupt_hash"]
        ):
            raise ExecutionConflict("native interaction moved beyond the accepted answer")
        current_messages(state, snapshot)


class NativeRunCommands:
    """Only this BFF capability can issue start, human resume and exact cancel calls."""

    def __init__(self, reader, releases, settings):
        """Use fixed deployment callback and published root declarations."""
        self.reader, self.releases, self.settings = reader, releases, settings

    async def submit(self, operation, snapshot):
        """Send one claimed immutable command outside any business transaction."""
        if (
            operation["status"] != "claimed"
            or digest(operation["request"]) != operation["request_hash"]
        ):
            raise ExecutionConflict("native command has no immutable sending right")
        self.releases.verify(snapshot)
        context = snapshot_context(snapshot, frozenset(operation["request"]["scopes"]))
        self.reader.store.execution.verify_context(context)
        request = operation["request"]
        if request["kind"] == "start":
            await self.reader.native.create_thread(snapshot["thread_id"])
            arguments = {"input": request["payload"]["input"]}
        elif request["kind"] == "resume":
            await self.reader.validate_resume(operation, snapshot)
            payload = request["payload"]
            arguments = {
                "command": {"resume": {payload["binding"]["interrupt_id"]: payload["response"]}},
                "checkpoint": payload["binding"]["checkpoint"],
            }
        else:
            raise ExecutionConflict("unsupported BFF root command")
        run = await self.reader.client.runs.create(
            snapshot["thread_id"],
            snapshot["assistant_id"],
            **arguments,
            context=context.model_dump(mode="json"),
            metadata={
                **context.trace_metadata(),
                "operation_id": operation["operation_id"],
                "application_run_id": context.run_id,
            },
            webhook=self.settings.bff_callback_url,
            multitask_strategy="reject",
        )
        return self.reader.reference(operation, snapshot, run["run_id"])

    async def cancel(self, reference):
        """Stop a known attempt; confirmation does not claim external side effects rolled back."""
        await self.reader.get_run(reference)
        thread_id, run_id = json.loads(reference.execution_id)
        return await self.reader.native.cancel_run(thread_id=thread_id, run_id=run_id)
