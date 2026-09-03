"""Thin product-to-Agent-Server API adapter."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from langgraph_sdk import get_client
from opentelemetry import trace


@dataclass(frozen=True, slots=True)
class ServerRun:
    run_id: str
    status: str


class AgentServerClient(Protocol):
    async def create_thread(self, thread_id: str) -> None: ...

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun: ...

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]: ...

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]: ...

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None: ...

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]: ...

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]: ...

    async def health(self) -> bool: ...


class LangGraphAgentServerClient:
    def __init__(
        self,
        *,
        url: str,
        service_token: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else None
        self._url = url.rstrip("/")
        self._headers = headers
        self._client = get_client(url=self._url, headers=headers, timeout=timeout_seconds)
        self._tracer = trace.get_tracer("financeclaw.agent_server")

    async def create_thread(self, thread_id: str) -> None:
        with self._tracer.start_as_current_span("agent_server.create_thread"):
            await self._client.threads.create(thread_id=thread_id, if_exists="do_nothing")

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        with self._tracer.start_as_current_span("agent_server.create_run"):
            run = await self._client.runs.create(
                thread_id,
                assistant_id,
                input=input,
                context=context,
                metadata=metadata,
            )
        return ServerRun(run_id=str(run["run_id"]), status=str(run.get("status", "pending")))

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        run = await self._client.runs.get(thread_id, run_id)
        if str(run.get("status")) not in {"success", "completed"}:
            return run

        # Recent Agent Server releases report a run that safely reached a
        # durable HITL checkpoint as ``success``.  The pending review lives on
        # the thread state instead.  Normalize that transport detail here so
        # the application service has one stable ``interrupted`` contract.
        state = await self._client.threads.get_state(thread_id)
        metadata = state.get("metadata", {})
        state_run_id = metadata.get("run_id") if isinstance(metadata, Mapping) else None
        if state_run_id == run_id and state.get("interrupts"):
            return {**run, "status": "interrupted", "interrupts": state["interrupts"]}
        return run

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        return await self._client.runs.join(thread_id, run_id)

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        runs = await self._client.runs.list(thread_id, limit=100)
        for run in runs:
            metadata = run.get("metadata", {})
            if (
                isinstance(metadata, Mapping)
                and metadata.get("application_run_id") == application_run_id
            ):
                return ServerRun(
                    run_id=str(run["run_id"]),
                    status=str(run.get("status", "pending")),
                )
        return None

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        return await self._client.runs.wait(
            thread_id,
            assistant_id,
            command=command,
            context=context,
            metadata=metadata,
        )

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        return self._client.threads.stream(
            thread_id,
            assistant_id=assistant_id,
            headers=self._headers,
        )

    async def health(self) -> bool:
        try:
            with self._tracer.start_as_current_span("agent_server.health"):
                async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
                    response = await client.get(f"{self._url}/ok", headers=self._headers)
            return response.is_success
        except httpx.HTTPError:
            return False
