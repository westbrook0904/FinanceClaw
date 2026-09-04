"""轻量 Run 编排服务（stage=1 直通通道）：解析目标并在 Agent Server 上执行。

在内存中维护业务 run 与 server run 的映射、幂等键索引；进程重启即失效，
需要持久化对账的会话与工作流路径分别使用 ConversationService 与 WorkflowService。
"""

import json
import logging
from asyncio import Lock
from collections.abc import AsyncIterator
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
from .streaming import (
    completed_stream_event,
    failed_stream_event,
    interrupted_stream_event,
    progress_stream_event,
    project_server_part,
)
from .target_resolver import ResolvedTarget, TargetResolutionError, TargetResolver

LOGGER = logging.getLogger(__name__)


class IdempotencyConflict(RuntimeError):
    """同一幂等键被用于内容不同的请求时抛出的冲突异常。"""

    pass


class RunNotFound(LookupError):
    """指定 run 不存在或不属于当前租户与主体时抛出。"""

    pass


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次已受理 Run 的完整映射记录（业务标识与 server 运行的绑定）。

    使用场景：RunService 内部用于幂等判定、归属校验与状态回写，
    并在恢复审批或订阅流式事件时提供 server 侧定位信息。

    Attributes:
        run_id: 业务 run ID，形如 "run-<uuid hex>"。
        server_run_id: Agent Server 端运行 ID。
        thread_id: 服务端会话线程 ID。
        tenant_id: 归属租户 ID，用于所有权校验。
        subject_id: 归属主体（用户）ID，用于所有权校验。
        assistant_id: 执行该 run 所用的服务端 assistant 标识。
        target_kind: 解析后的目标类型（"agent" 或 "tool"）。
        target_id: 目标业务 ID。
        target_version: 目标语义化版本号。
        fingerprint: 请求内容的 SHA-256 指纹，用于幂等键冲突检测。
        context: 随运行下发的执行上下文（租户、主体、权限范围等）。
        status: 最近一次已知的 server run 状态字符串。

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
    """轻量 Run 编排服务：受理 RunRequest 并在 Agent Server 上执行。

    使用场景：阶段一（stage=1）直通通道，供非会话用例直接运行 Agent 与工具
    目标；发布型 Workflow 不走本服务，必须使用持久化的 WorkflowService 通道。

    Attributes:
        client: Agent Server 客户端 Port。
        resolver: 目标解析器，把请求目标翻译为可执行描述。
        _by_idempotency: （私有）幂等键 (tenant_id, subject_id, key) 到记录的索引。
        _by_run_id: （私有）业务 run ID 到记录的映射。
        _lock: （私有）保护上述两个索引并发访问的 asyncio 锁。

    """

    def __init__(self, client: AgentServerClient, resolver: TargetResolver) -> None:
        """装配 Run 编排依赖。

        Args:
            client: Agent Server 客户端 Port。
            resolver: 目标解析器。

        """
        self.client = client
        self.resolver = resolver
        self._by_idempotency: dict[tuple[str, str, str], RunRecord] = {}
        self._by_run_id: dict[str, RunRecord] = {}
        self._lock = Lock()

    @staticmethod
    def _fingerprint(request: RunRequest) -> str:
        """对请求做规范化 JSON 序列化并计算 SHA-256 指纹。

        Args:
            request: 待摘要的 Run 请求。

        Returns:
            64 位十六进制指纹字符串。

        """
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
        """受理一次 Run 请求：解析目标、创建 server run 并登记幂等索引。

        Args:
            request: 待执行的 Run 请求（目标可为 None、Agent 或工具）。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，随执行上下文下发。
            idempotency_key: 客户端幂等键，重复提交需保持一致。

        Returns:
            受理结果：run/thread 标识、目标类型与是否幂等重放。

        Raises:
            TargetResolutionError: 目标为发布型 Workflow、无法解析或未被治理允许。
            IdempotencyConflict: 幂等键已被内容不同的请求占用。

        """
        if isinstance(request.target, WorkflowTarget):
            raise TargetResolutionError(
                "published workflows require the persistent WorkflowService path"
            )
        # 1. 计算请求指纹，作为幂等键冲突的判定依据。
        fingerprint = self._fingerprint(request)
        key = (tenant_id, subject_id, idempotency_key)
        # 2. 加锁保护幂等索引与 run 映射的并发读写。
        async with self._lock:
            existing = self._by_idempotency.get(key)
            if existing is not None:
                # 3. 幂等键已存在：指纹一致则重放，否则视为冲突。
                if existing.fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used for another request"
                    )
                return self._accepted(existing, replay=True)
            # 4. 首次请求：解析目标并创建映射记录。
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
        """在 Agent Server 上创建线程与运行，并组装映射记录。

        Args:
            target: 解析后的执行目标。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围。
            fingerprint: 请求指纹，写入映射记录。

        Returns:
            业务 run 与 server run 的映射记录。

        """
        # 1. 生成业务 run/thread/turn 标识与执行上下文。
        run_id = f"run-{uuid4().hex}"
        thread_id = str(uuid4())
        context = ExecutionContext(
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            turn_id=f"turn-{uuid4().hex}",
            run_id=run_id,
        )
        # 2. 组装服务端元数据：application_run_id 承载业务 run 映射。
        metadata = {
            **context.trace_metadata(),
            "stage": "1",
            "target_kind": target.kind,
            "target_id": target.target_id,
            "target_version": target.target_version,
        }
        metadata["application_run_id"] = metadata.pop("run_id")
        # 3. 创建线程并启动 server run。
        await self.client.create_thread(thread_id)
        server_run = await self.client.create_run(
            thread_id=thread_id,
            assistant_id=target.assistant_id,
            input=target.input,
            context=context.model_dump(mode="json"),
            metadata=metadata,
        )
        # 4. 返回完整的映射记录。
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
        """查询 run 的最新状态，并把 server 状态回写到内存记录。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            最新状态响应（输出仅透传可序列化载荷）。

        Raises:
            RunNotFound: run 不存在或不属于当前主体。

        """
        # 1. 校验归属并加载记录。
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        # 2. 查询服务端最新状态并回写内存记录。
        result = await self.client.get_run(
            thread_id=record.thread_id,
            run_id=record.server_run_id,
        )
        status = str(result.get("status", record.status))
        self._by_run_id[run_id] = replace(record, status=status)
        # 3. 仅透传可序列化的输出载荷。
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
        """提交审批决定并恢复被中断的 run。

        Args:
            run_id: 业务 run ID。
            decision: 审批决定（approve/reject/edit 及理由、参数 hash）。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            恢复后的最新状态响应。

        Raises:
            RunNotFound: run 不存在或不属于当前主体。

        """
        # 1. 校验归属并加载运行记录。
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        # 2. 组装审批决定：EDIT 附带编辑后的工具调用参数。
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
        # 3. 恢复服务端运行。
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
        # 4. 依据 "__interrupt__" 判定是继续等待审批还是已完成。
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
        """订阅指定 server run，并以权威终态查询校正流式结果。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Yields:
            归一化后的流式事件（事件名 + 数据载荷）。

        Raises:
            RunNotFound: run 不存在或不属于当前主体。

        """
        record = self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)
        stream_failed = False
        try:
            async for part in self.client.stream_run(
                thread_id=record.thread_id,
                run_id=record.server_run_id,
            ):
                projected = project_server_part(part)
                if projected is not None:
                    yield projected
        except Exception:
            # join_stream 不回放建立订阅前的片段；断流后继续查权威状态，不泄漏异常细节。
            stream_failed = True
            LOGGER.warning("Agent Server run stream ended unexpectedly", extra={"run_id": run_id})

        try:
            result = await self.client.get_run(
                thread_id=record.thread_id,
                run_id=record.server_run_id,
            )
            status = str(result.get("status", record.status))
            self._by_run_id[run_id] = replace(record, status=status)
            if status in {"success", "completed"}:
                output = await self.client.join_run(
                    thread_id=record.thread_id,
                    run_id=record.server_run_id,
                )
                yield completed_stream_event(run_id, output)
            elif status == "interrupted":
                yield interrupted_stream_event(run_id)
            elif status in {"error", "failed"} or stream_failed:
                yield failed_stream_event(run_id)
            else:
                yield progress_stream_event(run_id, status)
        except Exception:
            LOGGER.warning("Agent Server final run reconciliation failed", extra={"run_id": run_id})
            yield failed_stream_event(run_id)

    def _owned_record(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunRecord:
        """加载归属于当前租户与主体的运行记录。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            对应的映射记录。

        Raises:
            RunNotFound: 记录不存在或归属不匹配。

        """
        record = self._by_run_id.get(run_id)
        if record is None or record.tenant_id != tenant_id or record.subject_id != subject_id:
            raise RunNotFound("run was not found for authenticated owner")
        return record

    def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        """校验 run 归属于当前租户与主体，不通过则抛出 RunNotFound。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        """
        self._owned_record(run_id, tenant_id=tenant_id, subject_id=subject_id)

    @staticmethod
    def _accepted(record: RunRecord, *, replay: bool) -> RunAccepted:
        """把映射记录投影为受理响应。

        Args:
            record: 已创建或命中的映射记录。
            replay: 是否为幂等重放。

        Returns:
            受理响应对象。

        """
        return RunAccepted(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status,
            target_kind=record.target_kind,
            idempotent_replay=replay,
        )
