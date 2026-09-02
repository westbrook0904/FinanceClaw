"""Thin product-to-Agent-Server API adapter."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from langgraph_sdk import get_client


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
    def __init__(self, *, url: str, service_token: str | None = None) -> None:
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else None
        self._url = url.rstrip("/")
        self._headers = headers
        self._client = get_client(url=self._url, headers=headers)

    async def create_thread(self, thread_id: str) -> None:
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
        run = await self._client.runs.create(
            thread_id,
            assistant_id,
            input=input,
            context=context,
            metadata=metadata,
        )
        return ServerRun(run_id=str(run["run_id"]), status=str(run.get("status", "pending")))

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        return await self._client.runs.get(thread_id, run_id)

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
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(f"{self._url}/ok", headers=self._headers)
            return response.is_success
        except httpx.HTTPError:
            return False
