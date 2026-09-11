"""SDK-shaped test boundary, retaining real product admission and result transactions."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import update

from financeclaw.api.application.turns.bootstrap import build_turns
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from financeclaw.shared.turns.types import now

SCOPES = frozenset(
    {
        "market:read",
        "tools:read",
        "watchlist:write",
        "artifacts:read",
        "memory:read",
        "memory:write",
        "tools:approve",
        "portfolio:review",
        "workflows:approve",
    }
)


class NativeClient:
    """Only transport is synthetic; persisted product state is real."""

    def __init__(self):
        """Provide the init boundary for this test scenario."""
        self.runs = self
        self.threads = SimpleNamespace(get_state=self.get_state)
        self.calls, self.values, self.states, self.cancelled = [], {}, {}, []

    async def create(self, thread_id, assistant_id, **kwargs):
        """Provide the create boundary for this test scenario."""
        identifier = str(uuid4())
        self.calls.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
        run = {
            "run_id": identifier,
            "thread_id": thread_id,
            "metadata": kwargs["metadata"],
            "status": "running",
        }
        self.values[identifier] = run
        if "input" in kwargs:
            self.states[thread_id] = {"values": kwargs["input"]}
        return run

    async def get(self, thread_id, run_id):
        """Provide the get boundary for this test scenario."""
        return self.values[run_id]

    async def list(self, thread_id, *, limit, offset):
        """Provide the list boundary for this test scenario."""
        return [v for v in self.values.values() if v["thread_id"] == thread_id][
            offset : offset + limit
        ]

    async def get_state(self, thread_id, **kwargs):
        """Provide the get state boundary for this test scenario."""
        return self.states[thread_id]

    async def cancel(self, thread_id, run_id, **kwargs):
        """Provide the cancel boundary for this test scenario."""
        self.cancelled.append(run_id)
        self.values[run_id]["status"] = "interrupted"

    async def join(self, *args, **kwargs):
        """Provide the join boundary for this test scenario."""
        await asyncio.Event().wait()


@pytest_asyncio.fixture
async def runtime(tmp_path):
    """Provide the runtime boundary for this test scenario."""
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        turn_fallback_seconds=0.1,
        database_url=f"sqlite+pysqlite:///{tmp_path}/runtime.db",
        database_auto_create_schema=True,
        artifact_root=str(tmp_path / "artifacts"),
    )
    resources = build_resources(settings, enable_persistence=True)
    client = NativeClient()
    turns = build_turns(settings, resources, client=client)
    yield SimpleNamespace(
        turns=turns,
        resources=resources,
        client=client,
        releases=SimpleNamespace(agent_profiles=turns.releases.agents),
    )
    await turns.lifecycle.stop()
    resources.database.close()


async def tick(runtime):
    """One durable reconciliation pass without relying on a background polling clock."""
    with runtime.turns.sessions.begin() as session:
        session.execute(update(ConversationTurnRow).values(next_action_at=now()))
    lease = runtime.turns.store.claim_due("test")
    if lease is None:
        return
    error = None
    try:
        await runtime.turns.lifecycle._step(lease)
    except Exception as exc:
        error = type(exc).__name__
    finally:
        runtime.turns.store.release(lease, delay=0, error=error)


def final_state(runtime, accepted, *, content="final answer"):
    """Make one exact native command terminal with an addressable final checkpoint."""
    with runtime.turns.sessions() as session:
        turn = session.get(ConversationTurnRow, accepted.turn_id)
        command = session.get(TurnCommandRow, turn.current_command_id)
        native_id = command.native_run_id
        snapshot = turn.release_snapshot
        user = runtime.turns.journal.list_messages(turn.conversation_id)[0]
    runtime.client.values[native_id]["status"] = "success"
    state = {
        "metadata": {"run_id": native_id},
        "checkpoint": {
            "checkpoint_id": str(uuid4()),
            "checkpoint_ns": "",
            "thread_id": turn.thread_id,
        },
        "tasks": [],
        "next": [],
        "values": {
            "messages": [
                {"type": "human", "content": user.content, "id": snapshot["user_message_id"]},
                {"type": "ai", "content": content},
            ]
        },
    }
    runtime.client.states[turn.thread_id] = state
    return state


def waiting_state(runtime, accepted, *, interrupt_id="question-1", approval=False):
    """Provide the waiting state boundary for this test scenario."""
    state = final_state(runtime, accepted)
    if approval:
        call = {
            "id": interrupt_id,
            "name": "watchlist_add",
            "args": {"symbol": "AAPL", "note": "synthetic"},
        }
        payload = {
            "action_requests": [{"name": call["name"], "args": call["args"]}],
            "review_configs": [
                {"action_name": call["name"], "allowed_decisions": ["approve", "reject"]}
            ],
        }
    else:
        call = {
            "id": interrupt_id,
            "name": "request_user__clarification",
            "args": {"question": "请补充时制"},
        }
        payload = {
            "kind": "user_interaction",
            "schema_version": 1,
            "point_id": "clarification",
            "interaction_kind": "input",
            "question": "请补充时制",
        }
    state["values"]["messages"][-1] = {"type": "ai", "content": "", "tool_calls": [call]}
    state["tasks"] = [{"interrupts": [{"id": interrupt_id, "value": payload}]}]
    state["next"] = ["tools"]
    return state


OWNER = {"tenant_id": "tenant", "subject_id": "user"}


async def admit(runtime, *, conversation_id=None, key=None, message="hello"):
    """Accept a real product Turn without executing the scripted native client yet."""
    from financeclaw.kernel.responses import ConversationTurnRequest

    if conversation_id is None:
        conversation_id = runtime.turns.journal.create_conversation(
            **OWNER, agent_id="finance_agent", agent_profile_version="1.6.0"
        ).conversation_id
    return await runtime.turns.start_turn(
        conversation_id,
        ConversationTurnRequest(message=message),
        **OWNER,
        scopes=SCOPES,
        idempotency_key=key or str(uuid4()),
    )


async def question(runtime, accepted, *, interrupt_id="first", approval=False):
    """Observe a native interrupt through the production checkpoint validation path."""
    waiting_state(runtime, accepted, interrupt_id=interrupt_id, approval=approval)
    await tick(runtime)
    return (await runtime.turns.status(accepted.turn_id, **OWNER)).pending_interactions[0]


async def answer(runtime, item, *, key="answer", text="公历", scopes=SCOPES, decision=None):
    """Submit a typed input or action-bound approval using the current public revision."""
    from financeclaw.kernel.interactions import InteractionResponse

    data = {"revision": item["revision"], "kind": item["kind"]}
    if item["kind"] == "approval":
        data.update(decision=decision, action_hash=item["action_hash"])
    else:
        data.update(answer={"text": text})
    return await runtime.turns.interactions.respond(
        item["interaction_id"],
        InteractionResponse(**data),
        **OWNER,
        scopes=scopes,
        idempotency_key=key,
    )
