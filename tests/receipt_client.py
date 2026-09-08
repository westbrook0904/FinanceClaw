"""旧测试 Fake 的新提交回执适配，只用于测试，不在生产降级为 runs.wait。"""

from financeclaw.coordination.backends.ports.agent_server import ServerRun


class ReceiptClientMixin:
    """让每次假恢复也产生独立 Server Run，以便测试精确对账语义。"""

    async def submit_resume(self, **kwargs) -> ServerRun:
        """执行假恢复并保存新尝试，保留原尝试避免被最新线程状态掩盖。"""
        predecessor = kwargs.pop("predecessor", None)
        if predecessor is not None:
            assert predecessor in self.runs
        output = await self.resume_run(**kwargs)
        server_id = f"server-resume-{len(self.runs) + 1}"
        interrupts = output.get("__interrupt__", output.get("interrupts", ()))
        status = "interrupted" if interrupts else "success"
        self.runs[server_id] = {
            **kwargs,
            "run_id": server_id,
            "status": status,
            "output": output,
            "interrupts": interrupts,
        }
        return ServerRun(server_id, status)

    async def find_operation(self, *, thread_id: str, operation_id: str) -> ServerRun | None:
        """操作身份必须完整匹配，不能只匹配业务 run。"""
        matches = [
            (key, value)
            for key, value in self.runs.items()
            if value["thread_id"] == thread_id
            and value["metadata"].get("operation_id") == operation_id
        ]
        assert len(matches) <= 1
        return ServerRun(matches[0][0], matches[0][1]["status"]) if matches else None

    async def cancel_run(self, *, thread_id: str, run_id: str) -> bool:
        """假客户端明确确认停止，用于取消与新消息互斥测试。"""
        assert self.runs[run_id]["thread_id"] == thread_id
        self.runs[run_id]["status"] = "interrupted"
        return True
