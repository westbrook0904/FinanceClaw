"""薄的出站提交协调：准备／领取／回执／对账，不负责节点执行和队列调度。"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from financeclaw.modules.execution import ExecutionConflict, ExecutionRepository
from financeclaw.modules.execution.repository import digest

from .ports import AgentServerClient, ServerRun

LOGGER = logging.getLogger(__name__)


def agent_snapshot(
    profile: Any, context: Any, *, thread_id: str, input_hash: str
) -> dict[str, Any]:
    """固定实际档案、Schema、工具与执行身份，恢复时可与当前发布物比对。"""
    return {
        "context": context.model_dump(mode="json"),
        "thread_id": thread_id,
        "assistant_id": profile.execution_assistant_id,
        "input_hash": input_hash,
        "profile": profile.model_dump(mode="json"),
        "input_schema": profile.input_schema.model_json_schema() if profile.input_schema else None,
        "output_schema": profile.output_schema.model_json_schema()
        if profile.output_schema
        else None,
        "limits": {
            "model": profile.max_tree_model_calls,
            "tool": profile.max_tree_tool_calls,
            "operation": profile.max_tree_operations,
        },
    }


def verify_agent_snapshot(profile: Any, snapshot: dict[str, Any]) -> None:
    """旧代码与依赖不能共存时阻止恢复，不将旧版本记录静默交给新图。"""
    if (
        snapshot.get("profile") != profile.model_dump(mode="json")
        or snapshot.get("assistant_id") != profile.execution_assistant_id
        or snapshot.get("input_schema")
        != (profile.input_schema.model_json_schema() if profile.input_schema else None)
        or snapshot.get("output_schema")
        != (profile.output_schema.model_json_schema() if profile.output_schema else None)
    ):
        raise ExecutionConflict("pinned Agent release is unavailable; drain or reauthorize the run")


def json_value(value: Any) -> Any:
    """把框架消息与 interrupt 转成持久化 JSON，保持身份和业务结构。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value") and hasattr(value, "id"):
        return {"id": value.id, "value": json_value(value.value)}
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


class ExecutionService:
    """所有可能重复投递的 start/resume 共用持久化原子领取。"""

    def __init__(self, client: AgentServerClient, repository: ExecutionRepository) -> None:
        """注入出站 Port 与事务仓储，不持有内存锁作为正确性依据。"""
        self.client = client
        self.repository = repository

    async def submit(
        self,
        run_id: str,
        key: str,
        *,
        thread_id: str,
        assistant_id: str,
        context: dict[str, Any],
        metadata: dict[str, Any],
        input: dict[str, Any] | None = None,
        command: dict[str, Any] | None = None,
        predecessor: str | None = None,
    ) -> ServerRun | None:
        """首次提交或找回同一操作；未知回执保持等待，绝不自动重发。"""
        operation_id = f"operation-{digest([run_id, key])}"
        request = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "command": command,
            "predecessor": predecessor,
        }
        operation = await asyncio.to_thread(self.repository.prepare, operation_id, run_id, request)
        if operation["server_run_id"] is not None:
            return ServerRun(operation["server_run_id"], "pending")
        claimed = await asyncio.to_thread(self.repository.claim, operation_id)
        if claimed:
            try:
                kwargs = {
                    "thread_id": thread_id,
                    "assistant_id": assistant_id,
                    "context": context,
                    "metadata": {**metadata, "operation_id": operation_id},
                }
                server = (
                    await self.client.create_run(input=input, **kwargs)
                    if command is None
                    else await self.client.submit_resume(
                        command=command, predecessor=predecessor, **kwargs
                    )
                )
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.to_thread(self.repository.uncertain, operation_id))
                raise
            except Exception as exc:
                await asyncio.to_thread(self.repository.uncertain, operation_id)
                LOGGER.warning(
                    "execution submission requires reconciliation",
                    extra={"operation_id": operation_id, "error_type": type(exc).__name__},
                )
                return None
        else:
            server = await self.client.find_operation(
                thread_id=thread_id, operation_id=operation_id
            )
            if server is None:
                return None
        await asyncio.to_thread(self.repository.bind, operation_id, server.run_id)
        return server

    async def result(
        self, run_id: str, key: str, *, delegation_id: str | None = None, audit: Any = None
    ) -> dict[str, Any] | None:
        """只观察已受理尝试；完成、失败和中断统一交给调用方分类。"""
        operation_id = f"operation-{digest([run_id, key])}"
        operation = await asyncio.to_thread(self.repository.operation, operation_id)
        if operation["result"] is not None:
            return operation["result"]
        if operation["server_run_id"] is None:
            return None
        thread_id = operation["request"]["thread_id"]
        server_id = operation["server_run_id"]
        state = await self.client.get_run(thread_id=thread_id, run_id=server_id)
        if state.get("status") in {"pending", "running"}:
            return None
        if state.get("status") in {"success", "completed"}:
            output = await self.client.join_run(thread_id=thread_id, run_id=server_id)
            observed = {"status": "completed", **json_value(output)}
        elif state.get("status") in {"error", "failed", "interrupted", "timeout"}:
            observed = {
                "status": state["status"],
                "interrupts": json_value(state.get("interrupts", ())),
                "checkpoint": json_value(state.get("checkpoint")),
            }
        else:
            raise ExecutionConflict("unrecognized server attempt state")
        await asyncio.to_thread(
            self.repository.observe,
            operation_id,
            observed,
            delegation_id=delegation_id,
            audit=audit,
        )
        return observed

    async def reconcile(self, run_id: str) -> bool:
        """补绑定响应丢失的原操作；返回是否仍有无法证明提交结果的操作。"""
        operations = await asyncio.to_thread(self.repository.operations_for_run, run_id)
        uncertain = False
        for operation in operations:
            if operation["server_run_id"] is not None or operation["status"] == "prepared":
                continue
            found = await self.client.find_operation(
                thread_id=operation["request"]["thread_id"],
                operation_id=operation["operation_id"],
            )
            if found is None:
                uncertain = True
            else:
                await asyncio.to_thread(
                    self.repository.bind, operation["operation_id"], found.run_id
                )
        return uncertain
