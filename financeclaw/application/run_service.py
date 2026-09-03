"""提供无会话运行的启动、查询、审批恢复与事件流接口。"""

import json
from asyncio import Lock
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any
from uuid import uuid4

from financeclaw.kernel import (
    ApprovalDecision,
    ApprovalDecisionType,
    ExecutionContext,
    RunAccepted,
    RunRequest,
    RunStatusResponse,
    StreamEvent,
    WorkflowTarget,
)

from .ports import AgentServerClient
from .target_resolver import ResolvedTarget, TargetResolutionError, TargetResolver


class IdempotencyConflict(RuntimeError):
    """定义IdempotencyConflict。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class RunNotFound(LookupError):
    """定义运行NotFound。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


@dataclass(frozen=True, slots=True)
class RunRecord:
    """定义进程内无会话运行的所有权和远端关联。

    适用场景：
        用于跨步骤保存不可变事实，并支持持久化或审计重放。

    属性：
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        server_run_id: Agent Server 侧运行标识；尚未提交远端运行时为空。
        thread_id: Agent Server 线程标识，用于保存运行检查点与消息状态。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        assistant_id: 提交 Agent Server 时使用的助手或图标识。
        target_kind: 实际运行目标类别，用于调用方解释运行语义。
        target_id: 解析前或解析后的目标稳定标识。
        target_version: 运行实际绑定的目标版本，防止后续配置变化影响重放。
        fingerprint: 请求规范化后的指纹，用于识别幂等重放与冲突。
        context: 本次运行的租户、主体、权限与关联标识上下文。
        status: 当前生命周期状态，决定记录允许的后续操作。
    """

    run_id: str
    server_run_id: str
    thread_id: str
    tenant_id: str
    subject_id: str
    assistant_id: str
    target_kind: str
    target_id: str
    target_version: str
    fingerprint: str
    context: ExecutionContext
    status: str


class RunService:
    """管理不依赖持久化会话的短生命周期 Agent 或工具运行。

    适用场景：
        用于应用用例需要跨仓储、外部端口或领域策略协调一致结果的场景。

    属性：
        client: 负责与外部 Agent Server 或供应商通信的端口实现。
        resolver: 把外部目标请求解析为固定版本运行参数的解析器。
        _by_idempotency: 内部 `by idempotency` 状态或依赖，不属于公开接口。
        _by_run_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        _lock: 内部 `lock` 状态或依赖，不属于公开接口。
    """

    def __init__(self, client: AgentServerClient, resolver: TargetResolver) -> None:
        """注入并保存运行Service所需的协作对象，同时校验构造期不变量。"""
        self.client = client
        self.resolver = resolver
        self._by_idempotency: dict[tuple[str, str, str], RunRecord] = {}
        self._by_run_id: dict[str, RunRecord] = {}
        self._lock = Lock()

    @staticmethod
    def _fingerprint(request: RunRequest) -> str:
        """对规范化运行请求计算稳定哈希，用于幂等冲突检测。"""
        payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode()).hexdigest()

    async def start(
        self,
        request: RunRequest,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
    ) -> RunAccepted:
        """解析调用目标，利用请求指纹实现进程内幂等，然后创建服务端运行。"""
        if isinstance(request.target, WorkflowTarget):
            raise TargetResolutionError(
                "published workflows require the persistent WorkflowService path"
            )
        fingerprint = self._fingerprint(request)
        key = (tenant_id, subject_id, idempotency_key)
        async with self._lock:
            existing = self._by_idempotency.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used for another request"
                    )
                return self._accepted(existing, replay=True)
            resolved = self.resolver.resolve(request)
            record = await self._create_record(
                resolved,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
                fingerprint=fingerprint,
            )
            self._by_idempotency[key] = record
            self._by_run_id[record.run_id] = record
            return self._accepted(record, replay=False)

    async def _create_record(
        self,
        target: ResolvedTarget,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        fingerprint: str,
    ) -> RunRecord:
        """创建并返回新的运行Service。"""
        run_id = f"run-{uuid4().hex}"
        thread_id = str(uuid4())
        context = ExecutionContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            turn_id=f"turn-{uuid4().hex}",
            run_id=run_id,
        )
        metadata = {
            **context.trace_metadata(),
            "stage": "1",
            "target_kind": target.kind,
            "target_id": target.target_id,
            "target_version": target.target_version,
        }
        metadata["application_run_id"] = metadata.pop("run_id")
        await self.client.create_thread(thread_id)
        server_run = await self.client.create_run(
            thread_id=thread_id,
            assistant_id=target.assistant_id,
            input=target.input,
            context=context.model_dump(mode="json"),
            metadata=metadata,
        )
        return RunRecord(
            run_id=run_id,
            server_run_id=server_run.run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            assistant_id=target.assistant_id,
            target_kind=target.kind,
            target_id=target.target_id,
            target_version=target.target_version,
            fingerprint=fingerprint,
            context=context,
            status=server_run.status,
        )

    async def status(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        """读取调用主体拥有的运行，并返回服务端当前状态及可用输出。"""
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        result = await self.client.get_run(
            thread_id=record.thread_id,
            run_id=record.server_run_id,
        )
        status = str(result.get("status", record.status))
        self._by_run_id[run_id] = replace(record, status=status)
        output = result.get("output")
        if not isinstance(output, dict | list):
            output = None
        return RunStatusResponse(
            run_id=run_id,
            thread_id=record.thread_id,
            status=status,
            output=output,
        )

    async def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
    ) -> RunStatusResponse:
        """把人工审批决定转换为 LangGraph Command，恢复中断的服务端运行。"""
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        mapped: dict[str, Any] = {"type": decision.type.value}
        if decision.arguments_hash is not None:
            mapped["arguments_hash"] = decision.arguments_hash
        if decision.reason is not None:
            mapped["message"] = decision.reason
        if decision.type is ApprovalDecisionType.EDIT:
            mapped["edited_action"] = {
                "name": record.target_id,
                "args": decision.arguments,
            }
        result = await self.client.resume_run(
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
            command={"resume": {"decisions": [mapped]}},
            context=record.context.model_dump(mode="json"),
            metadata={
                **{
                    key: value
                    for key, value in record.context.trace_metadata().items()
                    if key != "run_id"
                },
                "application_run_id": record.context.run_id,
                "stage": "1",
            },
        )
        interrupted = bool(result.get("__interrupt__"))
        status = "interrupted" if interrupted else "completed"
        output: dict[str, Any] | list[Any] | None = dict(result)
        return RunStatusResponse(
            run_id=run_id,
            thread_id=record.thread_id,
            status=status,
            output=output,
        )

    async def stream(
        self, run_id: str, *, tenant_id: str, subject_id: str
    ) -> AsyncIterator[StreamEvent]:
        """校验运行所有权后转发服务端线程事件。"""
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        async for part in self.client.stream_thread(
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
        ):
            if isinstance(part, Mapping):
                event = str(part.get("event", "message"))
                data = part.get("data", dict(part))
            else:
                event = str(getattr(part, "event", "message"))
                data = getattr(part, "data", repr(part))
            yield StreamEvent(event=event, data=data)

    def _owned_record(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunRecord:
        """读取记录并同时校验租户与主体所有权，避免越权访问。"""
        record = self._by_run_id.get(run_id)
        if record is None or record.tenant_id != tenant_id or record.subject_id != subject_id:
            raise RunNotFound("run was not found for authenticated owner")
        return record

    def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        """验证运行Service满足当前边界要求，否则抛出明确异常。"""
        self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)

    @staticmethod
    def _accepted(record: RunRecord, *, replay: bool) -> RunAccepted:
        """把内部运行记录投影为已接受响应。"""
        return RunAccepted(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status,
            target_kind=record.target_kind,
            idempotent_replay=replay,
        )
