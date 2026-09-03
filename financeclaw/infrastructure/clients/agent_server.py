"""通过 LangGraph SDK 实现 Agent Server 应用端口。"""

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from langgraph_sdk import get_client
from opentelemetry import trace

from financeclaw.application.ports import ServerRun


class LangGraphAgentServerClient:
    """使用 LangGraph SDK 调用远端 Agent Server，并隐藏传输细节。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        _url: 内部 `url` 状态或依赖，不属于公开接口。
        _headers: 内部 `headers` 状态或依赖，不属于公开接口。
        _client: 负责与外部 Agent Server 或供应商通信的端口实现。
        _tracer: 内部 `tracer` 状态或依赖，不属于公开接口。
    """

    def __init__(
        self,
        *,
        url: str,
        service_token: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        """注入并保存LangGraphAgent服务端Client所需的协作对象，同时校验构造期不变量。"""
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else None
        self._url = url.rstrip("/")
        self._headers = headers
        self._client = get_client(url=self._url, headers=headers, timeout=timeout_seconds)
        self._tracer = trace.get_tracer("financeclaw.agent_server")

    async def create_thread(self, thread_id: str) -> None:
        """创建并返回新的LangGraphAgent服务端Client。"""
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
        """创建并返回新的LangGraphAgent服务端Client。"""
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
        """按标识读取LangGraphAgent服务端Client；不存在时由下层仓储抛出明确异常。"""
        run = await self._client.runs.get(thread_id, run_id)
        if str(run.get("status")) not in {"success", "completed"}:
            return run

        state = await self._client.threads.get_state(thread_id)
        metadata = state.get("metadata", {})
        state_run_id = metadata.get("run_id") if isinstance(metadata, Mapping) else None
        if state_run_id == run_id and state.get("interrupts"):
            return {**run, "status": "interrupted", "interrupts": state["interrupts"]}
        return run

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """等待指定服务端运行结束，并返回其最终结构化输出。"""
        return await self._client.runs.join(thread_id, run_id)

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """查找匹配的LangGraphAgent服务端Client；没有匹配项时返回空值。"""
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
        """使用审批决定恢复中断的LangGraphAgent服务端Client。"""
        return await self._client.runs.wait(
            thread_id,
            assistant_id,
            command=command,
            context=context,
            metadata=metadata,
        )

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        """校验访问权限后流式输出LangGraphAgent服务端Client事件。"""
        return self._client.threads.stream(
            thread_id,
            assistant_id=assistant_id,
            headers=self._headers,
        )

    async def health(self) -> bool:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
        try:
            with self._tracer.start_as_current_span("agent_server.health"):
                async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
                    response = await client.get(f"{self._url}/ok", headers=self._headers)
            return response.is_success
        except httpx.HTTPError:
            return False
