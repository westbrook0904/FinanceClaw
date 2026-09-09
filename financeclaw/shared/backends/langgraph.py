"""LangGraph Agent Server 出站客户端：把 Agent Server 的 thread/run 能力适配为应用层 Port。

共享客户端封装内部 SDK 连接、线程创建、操作回执查找和精确取消。
BFF 应用层负责原生运行命令与证据校验；健康检查不跟随重定向并使用独立短超时。
"""

import httpx
from langgraph_sdk import get_client
from opentelemetry import trace

from financeclaw.kernel.agent_server import ServerRun


class LangGraphAgentServerClient:
    """内部 LangGraph Agent Server 的 HTTP 客户端适配器。

    使用场景：由 HTTP 入口的 create_default_app 构造并注入应用层服务，用于创建会话
    线程、查找固定 operation 的回执、取消确切尝试以及健康检查。

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

    async def find_operation(self, *, thread_id: str, operation_id: str) -> ServerRun | None:
        """分页按 operation_id 对账，不能用相同业务 run 的另一次 resume 顶替。"""
        offset = 0
        matches = []
        while True:
            runs = await self._client.runs.list(thread_id, limit=100, offset=offset)
            matches.extend(
                run for run in runs if run.get("metadata", {}).get("operation_id") == operation_id
            )
            if len(runs) < 100:
                break
            offset += len(runs)
        if len(matches) > 1:
            raise RuntimeError("duplicate server attempts for one execution operation")
        return ServerRun(str(matches[0]["run_id"]), str(matches[0]["status"])) if matches else None

    async def cancel_run(self, *, thread_id: str, run_id: str) -> bool:
        """停止确切尝试；保留检查点，不删除线程、不伪装回滚外部副作用。"""
        state = await self._client.runs.get(thread_id, run_id)
        if state.get("status") in {"pending", "running"}:
            await self._client.runs.cancel(thread_id, run_id, wait=True, action="interrupt")
            state = await self._client.runs.get(thread_id, run_id)
        return state.get("status") in {"success", "error", "interrupted", "timeout"}

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
