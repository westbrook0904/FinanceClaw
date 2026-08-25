"""单进程 asyncio Basic Scheduler。

本文件负责把一个已经通过 ``PlanValidator`` 的静态 DAG 推进到最终状态或明确的
``WAITING`` 状态。核心职责包括：

* 维护 Plan/Node 的内存执行状态；
* 根据入边、Trigger 和 Condition 计算 READY/SKIPPED；
* 按 ``PlanBudget.max_concurrency`` 启动并回收 asyncio Task；
* 通过 ``CapabilityInvoker`` 执行节点，禁止直接访问 Registry/Provider；
* 处理 Join、fail-fast、continue-on-failure 和最终输出组合。

当前实现是阶段二 Basic Scheduler，不负责 Retry、持久化 Checkpoint、跨进程 Resume
或外部取消入口。这些可靠性能力会在后续里程碑复用这里的状态迁移边界继续扩展。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from harness_contracts import (
    CapabilityError,
    Continuation,
    EdgeTrigger,
    ExecutionPlan,
    FailurePolicy,
    InvocationContext,
    NodeExecutionState,
    NodeExecutionStatus,
    PlanEdge,
    PlanExecutionState,
    PlanExecutionStatus,
    PlanNode,
    PlanNodeKind,
    Request,
    RequestError,
    ResultEnvelope,
    ResultIssue,
    ResultOutput,
    ResultStatus,
)
from harness_runtime import CapabilityInvoker, InvocationLifecycle
from harness_trace import Span, SpanType, Tracer

from .resolution import (
    BindingResolutionError,
    ConditionEvaluator,
    InputResolver,
    JsonPointerResolutionError,
    resolve_json_pointer,
)


# 只有这些状态表示节点不会在当前调度轮次中继续执行。WAITING 不是终态：它需要
# Approval/Async completion 等外部事件后恢复，因此依赖它的后继节点仍需等待。
_TERMINAL_NODE_STATUSES = {
    NodeExecutionStatus.SUCCEEDED,
    NodeExecutionStatus.FAILED,
    NodeExecutionStatus.DENIED,
    NodeExecutionStatus.SKIPPED,
    NodeExecutionStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class SchedulerOutcome:
    """一次调度推进的不可变返回值。

    ``result`` 是面向调用方的统一结果；``state`` 是包含所有节点状态的内部执行快照。
    ExecutionEngine 会保存 state 的深拷贝，后续 StateStore 也将以它为持久化主体。
    """

    result: ResultEnvelope
    state: PlanExecutionState


class BasicScheduler:
    """确定性推进一个已验证 DAG，所有 Capability 调用均经过 Invoker。

    “确定性”主要指：READY 节点按 Plan 中的声明顺序启动，同一批完成任务也按该顺序
    应用结果。实际并行完成时间可以不同，但状态合并和 fail-fast 主错误选择保持稳定。
    """

    def __init__(
        self,
        invoker: CapabilityInvoker,
        tracer: Tracer,
        lifecycle: InvocationLifecycle,
        *,
        input_resolver: InputResolver | None = None,
        condition_evaluator: ConditionEvaluator | None = None,
    ) -> None:
        """注入 Scheduler 所需的受控执行组件。

        Invoker、Tracer 和 Lifecycle 必须属于同一组 Composition Root，否则 Trace
        parent 或 Span 收尾可能落入不同后端。Resolver/Evaluator 可替换以便独立测试，
        但替代实现仍应只解释结构化协议，不得执行任意表达式。
        """

        if not isinstance(invoker, CapabilityInvoker):
            raise TypeError("invoker must be CapabilityInvoker")
        if not isinstance(tracer, Tracer):
            raise TypeError("tracer must implement Tracer")
        if not isinstance(lifecycle, InvocationLifecycle):
            raise TypeError("lifecycle must be InvocationLifecycle")
        if invoker.tracer is not tracer or invoker.lifecycle is not lifecycle:
            raise ValueError("scheduler, invoker, and lifecycle must share one tracer")
        self._invoker = invoker
        self._tracer = tracer
        self._lifecycle = lifecycle
        self._input_resolver = input_resolver or InputResolver()
        self._condition_evaluator = condition_evaluator or ConditionEvaluator()

    async def run(
        self,
        request: Request,
        plan: ExecutionPlan,
        context: InvocationContext,
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> SchedulerOutcome:
        """推进 DAG 至最终或 WAITING 状态。

        调度循环遵循以下步骤：

        1. 将依赖已满足的 PENDING 节点推进为 READY 或 SKIPPED；
        2. 在并发额度内把 READY 节点变成 RUNNING 并创建 Task；
        3. 等待至少一个 Task 完成；
        4. 按 Plan 声明顺序合并节点结果；
        5. 若触发 fail-fast，取消剩余任务；否则继续下一轮。

        调用方取消当前协程时保留 ``asyncio.CancelledError``，同时先把 Scheduler
        已知的运行中和未启动节点收敛到 CANCELLED，避免留下悬空 Task。
        """

        # PlanExecutionState 与不可变 ExecutionPlan 分离。Plan 描述“要做什么”，
        # State 描述“已经执行到哪里”，从而为后续 checkpoint/resume 奠定边界。
        state = PlanExecutionState(
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            status=PlanExecutionStatus.RUNNING,
            nodes={node.node_id: NodeExecutionState(node_id=node.node_id) for node in plan.nodes},
        )
        # 以下索引都只从已验证 Plan 构造。node_order 用于消除 set/Task 完成顺序
        # 带来的不确定性；incoming 用于高效判断 Root、Join 和分支激活。
        nodes = {node.node_id: node for node in plan.nodes}
        node_order = {node.node_id: index for index, node in enumerate(plan.nodes)}
        incoming: dict[str, list[PlanEdge]] = {node_id: [] for node_id in nodes}
        for edge in plan.edges:
            incoming[edge.to_node].append(edge)
        # results 只保存已经返回 ResultEnvelope 的节点。Binding 和 Condition 都从
        # 这里读取显式上游结果，不共享 Capability 内部的可变对象。
        results: dict[str, ResultEnvelope] = {}
        # Task -> node_id 映射同时充当当前并发占用集合。
        running: dict[asyncio.Task[tuple[str, ResultEnvelope]], str] = {}
        # 第一个触发 FAIL_PLAN 的结果作为稳定的 Plan 主错误。
        abort_result: ResultEnvelope | None = None

        try:
            while True:
                # 一次调用可能连续传播多层 SKIPPED，因此 _advance_pending 内部会
                # 迭代到局部不动点，再把所有新 READY 节点交回主循环。
                self._advance_pending(state, incoming, results)
                ready = sorted(
                    (
                        node_id
                        for node_id, node_state in state.nodes.items()
                        if node_state.status is NodeExecutionStatus.READY
                    ),
                    key=node_order.__getitem__,
                )
                # 只启动并发额度允许的前 N 个 READY 节点。未获得额度的节点继续
                # 保持 READY，下轮在其他 Task 完成后再启动。
                available = plan.budget.max_concurrency - len(running)
                for node_id in ready[:available]:
                    node_state = state.nodes[node_id]
                    node_state.status = NodeExecutionStatus.RUNNING
                    node_state.attempt = 1
                    node_state.started_at = datetime.now(UTC)
                    task = asyncio.create_task(
                        self._execute_node(
                            request,
                            plan,
                            nodes[node_id],
                            context,
                            results,
                            parent=parent,
                            trace_enabled=trace_enabled,
                        )
                    )
                    running[task] = node_id

                # 没有活动 Task 且没有 READY 节点时，当前 Plan 已到达最终状态，
                # 或被 WAITING 节点阻塞，需要返回 ACCEPTED 给调用方。
                if not running:
                    if any(
                        item.status is NodeExecutionStatus.READY
                        for item in state.nodes.values()
                    ):
                        continue
                    break

                # FIRST_COMPLETED 让 Scheduler 及时释放并发额度并解锁下游节点，
                # 无需等待当前批次所有并行节点全部结束。
                done, _ = await asyncio.wait(
                    running,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # asyncio.wait 返回 set；排序后再应用结果，确保主错误和 State
                # 更新顺序不依赖哈希或事件循环内部顺序。
                completed = sorted(done, key=lambda task: node_order[running[task]])
                for task in completed:
                    node_id = running.pop(task)
                    _, result = await task
                    results[node_id] = result
                    should_abort = self._apply_node_result(
                        state,
                        nodes[node_id],
                        result,
                    )
                    if should_abort and abort_result is None:
                        abort_result = result

                # FAIL_PLAN 发生后不再启动任何新节点。已运行 Task 被主动取消，
                # PENDING/READY 节点也统一标为 CANCELLED。
                if abort_result is not None:
                    await self._cancel_running(running, state)
                    self._cancel_unstarted(state)
                    break
        except asyncio.CancelledError:
            # 外部 task cancellation 是控制流，不转换成普通 FAILED 结果。
            await self._cancel_running(running, state)
            self._cancel_unstarted(state)
            state.status = PlanExecutionStatus.CANCELLED
            state.updated_at = datetime.now(UTC)
            state.completed_at = state.updated_at
            raise
        except Exception:
            # 即便 Scheduler 自身出现未预期异常，也必须清理子 Task 后再向 Engine
            # 抛出，防止后台 Capability 在调用已经结束后继续运行。
            await self._cancel_running(running, state)
            self._cancel_unstarted(state)
            raise

        # 调度停止后统一组合最终 ResultEnvelope，再由该结果驱动 Plan 状态收尾。
        result = self._compose_result(plan, state, results, abort_result)
        self._finish_state(state, result)
        return SchedulerOutcome(result=result, state=state)

    def _advance_pending(
        self,
        state: PlanExecutionState,
        incoming: dict[str, list[PlanEdge]],
        results: dict[str, ResultEnvelope],
    ) -> None:
        """把当前可判定的 PENDING 节点推进到 READY 或 SKIPPED。

        Root 没有入边，可直接 READY。非 Root 必须等所有静态前驱到达终态后再做
        Join 判断：至少一条入边激活则 READY，否则说明没有分支选择该节点，标记
        SKIPPED。SKIPPED 本身也是终态，可能继续解锁下游 ALWAYS 边，因此需要循环。
        """

        changed = True
        while changed:
            changed = False
            for node_id, node_state in state.nodes.items():
                if node_state.status is not NodeExecutionStatus.PENDING:
                    continue
                edges = incoming[node_id]
                if not edges:
                    node_state.status = NodeExecutionStatus.READY
                    changed = True
                    continue
                # Join 必须等待全部前驱终态。不能因第一条成功边先到就提前启动，
                # 否则其他并行前驱的输出和失败状态尚未稳定。
                if not all(
                    state.nodes[edge.from_node].status in _TERMINAL_NODE_STATUSES
                    for edge in edges
                ):
                    continue
                activated = any(self._edge_activated(edge, state, results) for edge in edges)
                if activated:
                    node_state.status = NodeExecutionStatus.READY
                else:
                    node_state.status = NodeExecutionStatus.SKIPPED
                    node_state.completed_at = datetime.now(UTC)
                    self._touch(state)
                changed = True

    def _edge_activated(
        self,
        edge: PlanEdge,
        state: PlanExecutionState,
        results: dict[str, ResultEnvelope],
    ) -> bool:
        """判断一条边的 Trigger 与可选 Condition 是否同时成立。

        Trigger 只读取前驱 NodeExecutionStatus；Condition 再从显式 ResultEnvelope
        读取数据。COMPLETED 不包含 SKIPPED，而 ALWAYS 包含所有终态，这是二者的
        主要区别。
        """

        predecessor = state.nodes[edge.from_node]
        trigger_matches = {
            EdgeTrigger.SUCCESS: predecessor.status is NodeExecutionStatus.SUCCEEDED,
            EdgeTrigger.FAILED: predecessor.status is NodeExecutionStatus.FAILED,
            EdgeTrigger.DENIED: predecessor.status is NodeExecutionStatus.DENIED,
            EdgeTrigger.COMPLETED: predecessor.status
            in {
                NodeExecutionStatus.SUCCEEDED,
                NodeExecutionStatus.FAILED,
                NodeExecutionStatus.DENIED,
                NodeExecutionStatus.CANCELLED,
            },
            EdgeTrigger.ALWAYS: predecessor.status in _TERMINAL_NODE_STATUSES,
        }[edge.trigger]
        # Trigger 不匹配时无需计算 Condition，避免访问本分支不应依赖的输出。
        if not trigger_matches:
            return False
        return edge.condition is None or self._condition_evaluator.evaluate(
            edge.condition,
            results,
        )

    async def _execute_node(
        self,
        request: Request,
        plan: ExecutionPlan,
        node: PlanNode,
        context: InvocationContext,
        results: dict[str, ResultEnvelope],
        *,
        parent: Span | None,
        trace_enabled: bool,
    ) -> tuple[str, ResultEnvelope]:
        """执行单个 READY 节点，并返回尚未写入 Plan State 的统一结果。

        本方法运行在独立 asyncio Task 中。它负责 PLAN_NODE Span、输入解析和
        CapabilityInvoker 调用；共享 PlanExecutionState 仍由主调度协程串行更新，
        从而避免多个节点 Task 同时修改状态对象。
        """

        # PLAN_NODE 是 Registry/Policy/Capability Span 的父级，能在同一 Plan Trace
        # 中清楚地区分多个并行节点的完整受控调用链。
        node_span = (
            self._tracer.start_span(
                f"plan_node.{node.node_id}",
                SpanType.PLAN_NODE,
                parent=parent,
                attributes={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "node_kind": node.kind.value,
                },
            )
            if trace_enabled
            else None
        )
        # Approval 是 ExecutionEngine 原生等待语义，不是 Registry Capability。
        # 当前 Basic Scheduler 先产生 Continuation；审批请求与 resume 在后续实现。
        if node.kind is PlanNodeKind.APPROVAL:
            result = ResultEnvelope.accepted(
                Continuation(
                    plan_id=plan.plan_id,
                    node_id=node.node_id,
                    waiting_reason="approval",
                )
            )
            self._lifecycle.finish_from_result(node_span, result)
            return node.node_id, result

        try:
            # 所有跨节点数据都在调用前解析成新的 RequestInput。Invoker 不需要理解
            # Plan Binding，也不会获得其他节点的状态对象。
            node_input = self._input_resolver.resolve(
                request,
                node.input_mapping,
                results,
            )
            # Scheduler 禁止 registry.resolve 后裸调 Provider；统一边界确保每个节点
            # 都经过 PRE_EXECUTE Policy、Trace、timeout 和错误归一化。
            result = await self._invoker.invoke(
                node.capability or "",
                node_input,
                context,
                timeout_ms=node.timeout_ms,
                parent=node_span,
                trace_enabled=trace_enabled,
            )
        except asyncio.CancelledError:
            # 内部 fail-fast 或外部取消都必须以 CANCELLED 结束 PLAN_NODE Span。
            self._lifecycle.finish_cancelled(node_span)
            raise
        except BindingResolutionError as exc:
            # Binding 属于 Plan 输入问题，归类为 REQUEST，而不是 Provider 执行失败。
            error = RequestError(
                "plan node input binding failed",
                code="HARNESS.PLAN.BINDING_FAILED",
                details={"plan_id": plan.plan_id, "node_id": node.node_id, "reason": str(exc)},
            )
            result = ResultEnvelope.failure(error.to_detail())
        except Exception as exc:
            # Invoker 正常会返回 ResultEnvelope；这里是节点适配层的最后防线。
            error = CapabilityError(
                "plan node execution failed",
                code="HARNESS.PLAN.NODE_FAILED",
                details={
                    "plan_id": plan.plan_id,
                    "node_id": node.node_id,
                    "cause_type": type(exc).__name__,
                },
            )
            result = ResultEnvelope.failure(error.to_detail())

        self._lifecycle.finish_from_result(node_span, result)
        return node.node_id, result

    def _apply_node_result(
        self,
        state: PlanExecutionState,
        node: PlanNode,
        result: ResultEnvelope,
    ) -> bool:
        """把节点 ResultEnvelope 映射为 Node State，并返回是否触发 fail-fast。

        Provider 的 PARTIAL 仍表示该节点产生了可供后继使用的输出，因此节点状态为
        SUCCEEDED，同时把局部 issues 提升到 Plan State。ACCEPTED 则映射为 WAITING，
        保存 Continuation，且不填写 completed_at。
        """

        node_state = state.nodes[node.node_id]
        node_state.result = result
        node_state.completed_at = datetime.now(UTC)
        if result.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}:
            node_state.status = NodeExecutionStatus.SUCCEEDED
        elif result.status is ResultStatus.FAILED:
            node_state.status = NodeExecutionStatus.FAILED
            node_state.error = result.error
        elif result.status is ResultStatus.DENIED:
            node_state.status = NodeExecutionStatus.DENIED
            node_state.error = result.error
        elif result.status is ResultStatus.CANCELLED:
            node_state.status = NodeExecutionStatus.CANCELLED
            node_state.error = result.error
        else:
            node_state.status = NodeExecutionStatus.WAITING
            node_state.completed_at = None
            node_state.waiting_reason = result.continuation.waiting_reason
            node_state.continuation = result.continuation

        # PlanExecutionState.issues 聚合所有局部问题，最终用于判断 SUCCESS/PARTIAL。
        if result.status is ResultStatus.PARTIAL:
            state.issues.extend(result.issues)
        elif result.status in {ResultStatus.FAILED, ResultStatus.DENIED} and result.error:
            state.issues.append(ResultIssue(source=node.node_id, error=result.error))
        self._touch(state)
        # 只有终止类结果与节点 FAIL_PLAN 同时出现才中止整个 DAG。CONTINUE 节点
        # 保留问题后允许 Scheduler 继续寻找其他可执行分支。
        return (
            result.status
            in {ResultStatus.FAILED, ResultStatus.DENIED, ResultStatus.CANCELLED}
            and node.failure_policy is FailurePolicy.FAIL_PLAN
        )

    async def _cancel_running(
        self,
        running: dict[asyncio.Task[tuple[str, ResultEnvelope]], str],
        state: PlanExecutionState,
    ) -> None:
        """取消当前运行 Task，等待清理完成，并同步节点状态。"""

        tasks = tuple(running)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for node_id in running.values():
            node_state = state.nodes[node_id]
            node_state.status = NodeExecutionStatus.CANCELLED
            node_state.completed_at = datetime.now(UTC)
        running.clear()
        self._touch(state)

    def _cancel_unstarted(self, state: PlanExecutionState) -> None:
        """把 fail-fast 后不再允许启动的 PENDING/READY 节点标为 CANCELLED。"""

        for node_state in state.nodes.values():
            if node_state.status in {
                NodeExecutionStatus.PENDING,
                NodeExecutionStatus.READY,
            }:
                node_state.status = NodeExecutionStatus.CANCELLED
                node_state.completed_at = datetime.now(UTC)
        self._touch(state)

    def _compose_result(
        self,
        plan: ExecutionPlan,
        state: PlanExecutionState,
        results: dict[str, ResultEnvelope],
        abort_result: ResultEnvelope | None,
    ) -> ResultEnvelope:
        """根据 Plan State 和 outputs mapping 组合最终 ResultEnvelope。

        优先级为：主终止错误 → WAITING → CANCELLED → 输出映射。只有全部必需输出
        都能解析时才会产生 SUCCESS/PARTIAL；正常条件分支形成的 SKIPPED 不会单独
        造成 PARTIAL，但如果 outputs 指向被跳过节点，则输出不可用并返回 FAILED。
        """

        metadata = {"plan_id": plan.plan_id, "plan_revision": plan.revision}
        # fail-fast 保留原始失败/拒绝/取消类别，不把 DENIED 错误降级成 FAILED。
        if abort_result is not None:
            if abort_result.status is ResultStatus.DENIED:
                return ResultEnvelope.denied(abort_result.error, metadata=metadata)
            if abort_result.status is ResultStatus.CANCELLED:
                return ResultEnvelope.cancelled(error=abort_result.error, metadata=metadata)
            return ResultEnvelope.failure(abort_result.error, metadata=metadata)

        # WAITING Plan 返回 ACCEPTED 和可定位的 Continuation，不长时间占用 API Task。
        waiting = tuple(
            item for item in state.nodes.values() if item.status is NodeExecutionStatus.WAITING
        )
        if waiting:
            continuation = waiting[0].continuation or Continuation(
                plan_id=plan.plan_id,
                node_id=waiting[0].node_id,
                waiting_reason=waiting[0].waiting_reason or "waiting",
            )
            return ResultEnvelope.accepted(continuation, metadata=metadata)

        if any(
            item.status is NodeExecutionStatus.CANCELLED for item in state.nodes.values()
        ):
            return ResultEnvelope.cancelled(metadata=metadata)

        # 最终输出仍使用与节点输入相同的显式 JSON Pointer 机制，禁止从共享对象
        # 隐式读取。任一必需输出缺失都会使整个 Plan 失败。
        output_data: dict[str, object] = {}
        try:
            for output_name, binding in plan.outputs.items():
                result = results.get(binding.node_id)
                if result is None:
                    raise BindingResolutionError(
                        f"output node result is unavailable: {binding.node_id}"
                    )
                output_data[output_name] = resolve_json_pointer(
                    result.model_dump(mode="json"),
                    binding.pointer,
                )
        except (BindingResolutionError, JsonPointerResolutionError) as exc:
            error = RequestError(
                "plan output mapping failed",
                code="HARNESS.PLAN.OUTPUT_UNAVAILABLE",
                details={"plan_id": plan.plan_id, "reason": str(exc)},
            )
            return ResultEnvelope.failure(error.to_detail(), metadata=metadata)

        output = ResultOutput(type="plan", data=output_data)
        # CONTINUE 失败或节点 PARTIAL 会留下 issues；输出完整时最终语义为 PARTIAL。
        if state.issues:
            return ResultEnvelope.partial(output, state.issues, metadata=metadata)
        return ResultEnvelope.success(output, metadata=metadata)

    def _finish_state(self, state: PlanExecutionState, result: ResultEnvelope) -> None:
        """用最终 ResultStatus 收敛 Plan 状态和完成时间。"""

        state.status = {
            ResultStatus.SUCCESS: PlanExecutionStatus.SUCCEEDED,
            ResultStatus.PARTIAL: PlanExecutionStatus.PARTIAL,
            ResultStatus.FAILED: PlanExecutionStatus.FAILED,
            ResultStatus.DENIED: PlanExecutionStatus.DENIED,
            ResultStatus.CANCELLED: PlanExecutionStatus.CANCELLED,
            ResultStatus.ACCEPTED: PlanExecutionStatus.WAITING,
        }[result.status]
        state.updated_at = datetime.now(UTC)
        if result.status is not ResultStatus.ACCEPTED:
            state.completed_at = state.updated_at
        state.state_version += 1

    def _touch(self, state: PlanExecutionState) -> None:
        """记录一次可持久化状态变化，预留 StateStore 版本演进边界。"""

        state.updated_at = datetime.now(UTC)
        state.state_version += 1
