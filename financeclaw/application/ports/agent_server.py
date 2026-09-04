"""Agent Server 出站 Port：应用层访问 LangGraph Agent Server 的最小通信契约。"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ServerRun:
    """Agent Server 端一次运行的最小快照。

    使用场景：create_run 的返回值与 find_run 的检索结果，供应用层把业务
    run 与 server run 建立映射，并同步运行状态。

    Attributes:
        run_id: 服务端运行 ID，用于后续查询、等待与恢复。
        status: 服务端运行状态字符串（如 pending、running、success、interrupted）。

    """

    run_id: str
    status: str


class AgentServerClient(Protocol):
    """应用层与 Agent Server 之间的出站客户端协议（结构化接口）。

    使用场景：由基础设施层实现（如 LangGraph SDK 适配器），application 层的
    Run、Conversation、Workflow 与 Delegation 服务仅依赖本协议完成 thread/run
    的创建、查询、恢复与流式订阅，不感知具体传输细节。
    """

    async def create_thread(self, thread_id: str) -> None:
        """按给定 ID 在 Agent Server 上创建会话线程。

        Args:
            thread_id: 目标线程 ID（由应用层生成，如业务 Agent thread）。

        """
        pass

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        """在指定线程上以给定 assistant 启动一次新的服务端运行。

        Args:
            thread_id: 目标线程 ID。
            assistant_id: 服务端 assistant（编译后的 LangGraph 图）标识。
            input: 发给 Agent 的初始输入载荷（如 {"messages": [...]}）。
            context: ExecutionContext 序列化结果，随运行下发租户/主体/权限上下文。
            metadata: 写入运行元数据的追踪字段（含 application_run_id 等映射）。

        Returns:
            新建运行的 ID 与初始状态快照。

        """
        pass

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """查询服务端运行的当前状态与元信息，不等待其结束。

        Args:
            thread_id: 目标线程 ID。
            run_id: 服务端运行 ID。

        Returns:
            运行详情映射（含 status、interrupts 等字段）。

        """
        pass

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """等待服务端运行结束并取回其最终输出。

        Args:
            thread_id: 目标线程 ID。
            run_id: 服务端运行 ID。

        Returns:
            运行结束后的状态快照（含 messages 等字段）。

        """
        pass

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """按 application_run_id 在线程内检索既有运行，用于重放与对账复用。

        Args:
            thread_id: 目标线程 ID。
            application_run_id: 业务侧 run ID（记录在运行元数据中）。

        Returns:
            找到时返回运行快照；线程内不存在时返回 None。

        """
        pass

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        """以命令（如审批决定）恢复被中断的服务端运行。

        Args:
            thread_id: 目标线程 ID。
            assistant_id: 服务端 assistant（编译后的 LangGraph 图）标识。
            command: 恢复命令（如 {"resume": {"decisions": [...]}}）。
            context: ExecutionContext 序列化结果，随恢复调用下发。
            metadata: 写入运行元数据的追踪字段。

        Returns:
            恢复后的运行输出；含 "__interrupt__" 键表示仍在等待下一次审批。

        """
        pass

    def stream_run(self, *, thread_id: str, run_id: str) -> AsyncIterator[Any]:
        """订阅指定服务端运行的流式事件序列。

        Args:
            thread_id: 目标线程 ID。
            run_id: 服务端运行 ID，避免共享线程上的多个 Turn 发生事件串流。

        Returns:
            异步事件迭代器。

        """
        pass

    async def health(self) -> bool:
        """探测 Agent Server 是否可用。

        Returns:
            服务可用时返回 True，否则返回 False。

        """
        pass
