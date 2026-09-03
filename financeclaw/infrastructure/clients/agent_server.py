"""LangGraph Agent Server 出站客户端：把 Agent Server 的 thread/run 能力适配为应用层 Port。

本模块属于 infrastructure 层，基于 ``langgraph_sdk`` 实现
``AgentServerClient`` 协议；所有调用经 OTel tracer 标注，健康检查不跟随
重定向并使用独立短超时。
"""

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from langgraph_sdk import get_client
from opentelemetry import trace

from financeclaw.application.ports import ServerRun


class LangGraphAgentServerClient:
    """内部 LangGraph Agent Server 的 HTTP 客户端适配器。

    使用场景：由 bootstrap.py 组合根构造并注入应用层服务，用于创建会话
    线程、发起与恢复运行、查询运行状态、订阅流式输出以及健康检查；
    应用层仅依赖 ``AgentServerClient`` 协议，不感知 SDK 细节。

    Attributes:
        _url: 规范化（去尾部斜杠）后的 Agent Server 基地址。
        _headers: 携带服务间 Bearer 令牌的请求头；未配置令牌时为 None。
        _client: langgraph_sdk 异步客户端，统一携带鉴权头与超时配置。
        _tracer: OTel tracer，为每次出站调用创建子 span 以串联链路。

    """

    def __init__(
        self,
        *,
        url: str,
        service_token: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        """构建 Agent Server 客户端。

        Args:
            url: Agent Server 基地址（启动时已通过内部主机 allowlist 校验）。
            service_token: 服务间 Bearer 令牌，生产环境必填。
            timeout_seconds: SDK 客户端的整体请求超时（秒）。

        """
        # 1. 组装鉴权头：仅当配置了服务令牌时携带。
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else None
        # 2. 创建 SDK 客户端与模块级 tracer。
        self._url = url.rstrip("/")
        self._headers = headers
        self._client = get_client(url=self._url, headers=headers, timeout=timeout_seconds)
        self._tracer = trace.get_tracer("financeclaw.agent_server")

    async def create_thread(self, thread_id: str) -> None:
        """幂等创建会话线程：线程已存在时不报错（``if_exists="do_nothing"``）。

        Args:
            thread_id: 应用层预先分配的线程 ID。

        """
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
        """在指定线程上以助手身份发起一次运行。

        Args:
            thread_id: 目标线程 ID。
            assistant_id: Agent Server 侧的助手（图）ID。
            input: 运行输入载荷。
            context: 运行上下文（租户、主体、数据分级等）。
            metadata: 运行元数据（含 application_run_id，供反查与审计）。

        Returns:
            业务侧运行快照（run_id 与初始状态）。

        """
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
        """查询运行状态；已完成的运行会进一步检查线程上的挂起中断。

        使用场景：轮询运行结果时，若服务端把中断以线程状态而非运行状态
        表达，需要在此统一转译为 ``interrupted``，让上层感知人工审批点。

        Args:
            thread_id: 目标线程 ID。
            run_id: 目标运行 ID。

        Returns:
            运行信息映射；若该运行产生了待处理中断，则状态改写为
            ``interrupted`` 并附带 ``interrupts`` 字段。

        """
        run = await self._client.runs.get(thread_id, run_id)
        # 1. 运行未完成时直接返回原始状态。
        if str(run.get("status")) not in {"success", "completed"}:
            return run

        # 2. 已完成：读取线程状态，确认中断是否由本次运行触发。
        state = await self._client.threads.get_state(thread_id)
        metadata = state.get("metadata", {})
        state_run_id = metadata.get("run_id") if isinstance(metadata, Mapping) else None
        # 3. 线程状态归属本次运行且存在中断时，转译为 interrupted 透出。
        if state_run_id == run_id and state.get("interrupts"):
            return {**run, "status": "interrupted", "interrupts": state["interrupts"]}
        return run

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """阻塞等待指定运行结束并返回其最终输出。

        Args:
            thread_id: 目标线程 ID。
            run_id: 目标运行 ID。

        Returns:
            运行结束后的结果映射。

        """
        return await self._client.runs.join(thread_id, run_id)

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """在线程的最近运行列表中按业务侧运行 ID 反查服务端运行。

        使用场景：服务重启或响应丢失后恢复状态，需要把业务 run 映射回
        服务端 run 而不重新发起运行。

        Args:
            thread_id: 目标线程 ID。
            application_run_id: 发起运行时写入 metadata 的业务侧运行 ID。

        Returns:
            匹配到的运行快照；最近 100 条中未找到时返回 None。

        """
        runs = await self._client.runs.list(thread_id, limit=100)
        # 逐条比对 metadata 中的 application_run_id 定位目标运行。
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
        """以命令（如人工审批决策）恢复被中断的运行并等待其完成。

        Args:
            thread_id: 目标线程 ID。
            assistant_id: Agent Server 侧的助手（图）ID。
            command: 恢复命令（如 resume 指令与决策载荷）。
            context: 运行上下文。
            metadata: 运行元数据。

        Returns:
            恢复后运行完成的结果映射。

        """
        return await self._client.runs.wait(
            thread_id,
            assistant_id,
            command=command,
            context=context,
            metadata=metadata,
        )

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        """订阅线程的流式输出事件。

        Args:
            thread_id: 目标线程 ID。
            assistant_id: Agent Server 侧的助手（图）ID。

        Returns:
            异步事件流迭代器；显式携带鉴权头以支持流式端点。

        """
        return self._client.threads.stream(
            thread_id,
            assistant_id=assistant_id,
            headers=self._headers,
        )

    async def health(self) -> bool:
        """探测 Agent Server 健康状态（请求 ``/ok`` 端点）。

        使用场景：就绪检查与后台轮询；使用独立的 2 秒短超时且不跟随
        重定向，避免健康检查被异常重定向放大为长阻塞。

        Returns:
            服务可用返回 True；网络错误或非成功状态返回 False。

        """
        try:
            with self._tracer.start_as_current_span("agent_server.health"):
                # follow_redirects=False：健康检查不跟随重定向，防止 SSRF 类风险。
                async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
                    response = await client.get(f"{self._url}/ok", headers=self._headers)
            return response.is_success
        except httpx.HTTPError:
            return False
