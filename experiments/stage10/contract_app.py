"""Synthetic graph and custom routes for the production runtime contract probe."""

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from typing import TypedDict

from fastapi import FastAPI
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langgraph_sdk import get_client


class ProbeState(TypedDict, total=False):
    """Only synthetic data crosses this probe's isolated runtime."""

    question: str
    delay: float
    answer: str
    worker: str


async def execute(state: ProbeState) -> dict:
    """Record the executing container and optionally create a durable wait."""
    await asyncio.sleep(state.get("delay", 0))
    answer = interrupt(state["question"]) if state.get("question") else "done"
    return {"answer": answer, "worker": socket.gethostname()}


builder = StateGraph(ProbeState)
builder.add_node("execute", execute)
builder.add_edge(START, "execute")
builder.add_edge("execute", END)
graph = builder.compile()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Expose evidence that custom lifespan also runs in the worker role."""
    app.state.role = os.environ["FINANCECLAW_PROCESS_ROLE"]
    app.state.native = get_client(url=None, api_key=None)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/probe/identity")
async def identity():
    """Identify the API process without relying on container logs."""
    return {"role": app.state.role, "host": socket.gethostname()}


@app.post("/probe/start/{thread_id}")
async def start(thread_id: str, payload: dict):
    """Use the public SDK loopback path to create a thread and run together."""
    return await app.state.native.runs.create(
        thread_id,
        "probe",
        input=payload,
        if_not_exists="create",
        multitask_strategy="reject",
        metadata={"probe_command": thread_id},
    )


@app.post("/probe/resume/{thread_id}")
async def resume(thread_id: str, payload: dict):
    """Restore an exact native interrupt using its complete checkpoint."""
    return await app.state.native.runs.create(
        thread_id,
        "probe",
        command={"resume": payload["answer"]},
        checkpoint=payload["checkpoint"],
        if_not_exists="reject",
        multitask_strategy="reject",
    )


@app.get("/probe/state/{thread_id}")
async def state(thread_id: str):
    """Read native persisted state via the in-process client."""
    return await app.state.native.threads.get_state(thread_id)


@app.get("/probe/join/{thread_id}/{run_id}")
async def join(thread_id: str, run_id: str):
    """Wait on the exact existing run without scheduling another execution."""
    await app.state.native.runs.join(thread_id, run_id)
    return await app.state.native.runs.get(thread_id, run_id)
