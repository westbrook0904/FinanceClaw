"""定义应用层访问 Agent Server 所需的最小异步端口。"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ServerRun:
    """定义Agent Server 创建运行后返回的最小引用。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        status: 当前生命周期状态，决定记录允许的后续操作。
    """

    run_id: str
    status: str


class AgentServerClient(Protocol):
    """约束应用层对 Agent Server 的线程、运行、恢复和流式访问能力。

    适用场景：
        用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。
    """

    async def create_thread(self, thread_id: str) -> None:
        """创建并返回新的Agent服务端Client。"""

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        """创建并返回新的Agent服务端Client。"""

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """按标识读取Agent服务端Client；不存在时由下层仓储抛出明确异常。"""

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """等待指定服务端运行结束，并返回其最终结构化输出。"""

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """查找匹配的Agent服务端Client；没有匹配项时返回空值。"""

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        """使用审批决定恢复中断的Agent服务端Client。"""

    def stream_thread(self, *, thread_id: str, assistant_id: str) -> AsyncIterator[Any]:
        """校验访问权限后流式输出Agent服务端Client事件。"""

    async def health(self) -> bool:
        """调用轻量健康端点，返回依赖服务当前是否可用。"""
