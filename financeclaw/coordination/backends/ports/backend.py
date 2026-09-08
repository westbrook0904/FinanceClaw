"""Stage-8 最小 Backend Port；旧 AgentServerClient 暂留给尚未迁移的入口。"""

from typing import Protocol

from financeclaw.kernel.coordination import (
    BackendCapabilities,
    BackendExecutionRef,
    BackendNotification,
    BackendObservation,
    CancellationReceipt,
    ResponseDelivery,
    SubmissionReceipt,
    TaskSubmission,
)


class AgentBackend(Protocol):
    """Worker 的唯一出站边界；身份、授权与命令领取在调用前由业务层复验。"""

    capabilities: BackendCapabilities

    async def submit_task(self, command: TaskSubmission) -> SubmissionReceipt:
        """提交已唯一领取的固定操作；未知结果不能自动重发。"""
        ...

    async def observe_execution(self, reference: BackendExecutionRef) -> BackendObservation:
        """只观察指定尝试；不足以关联的证据返回 unknown。"""
        ...

    async def deliver_response(self, command: ResponseDelivery) -> SubmissionReceipt:
        """向原 continuation 交付固定响应，产生独立的恢复尝试。"""
        ...

    async def request_cancel(
        self, reference: BackendExecutionRef, *, operation_id: str
    ) -> CancellationReceipt:
        """请求停止确切尝试；只有证据充分才返回 confirmed。"""
        ...

    async def lookup_operation(
        self, command: TaskSubmission | ResponseDelivery
    ) -> SubmissionReceipt:
        """按原操作查回执；没有记录仅为 uncertain，不授予重发权。"""
        ...

    def decode_notification(self, authenticated_body: bytes) -> BackendNotification:
        """解析已认证的有界载荷，无远程 I/O；认证属于 Ingress 的部署边界。"""
        ...
