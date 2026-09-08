"""跨进程执行事实仓储：CAS 领取、单调回执、持久预算与恢复授权。"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow


class ExecutionConflict(RuntimeError):
    """执行关联、快照、预算或取消状态不允许继续派发。"""


def digest(value: Any) -> str:
    """对已校验的 JSON 内容计算稳定摘要，不使用不稳定的对象 repr。"""
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot_context(
    snapshot: Mapping[str, Any], scopes: frozenset[str] | None = None
) -> ExecutionContext:
    """恢复权限最多为原授权上界；显式空集合意味着撤权，不等同于未提供。"""
    if not isinstance(snapshot.get("context"), Mapping):
        raise ExecutionConflict(
            "execution authorization snapshot is missing; reauthorization required"
        )
    original = ExecutionContext.model_validate(snapshot["context"])
    if scopes is None:
        return original
    effective = (
        scopes
        if "*" in original.scopes
        else original.scopes
        if "*" in scopes
        else original.scopes & scopes
    )
    return original.model_copy(update={"scopes": effective})


def _execution(row: RunExecutionRow) -> dict[str, Any]:
    """将 ORM 行导出为独立快照，避免事务外延迟加载。"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _operation(row: RunOperationRow) -> dict[str, Any]:
    """导出操作关联与状态供应用层对账。"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class ExecutionRepository:
    """持久化运行授权、出站操作、根任务预算和取消事实。

    run_executions 保存每个业务 run 的冻结快照，run_operations 保存其
    各次 start/resume 命令。领取操作与扣减根预算在同一事务中完成，
    子任务、恢复和重试因此不能各自获得一份新的根预算。

    方法返回脱离 Session 的字典，供可信应用层使用；涉及用户访问时，
    调用方仍需校验归属。接受 session 的方法可以参与交互决定等外层事务，
    其余写方法自行提交短事务，所有远程 I/O 均由应用服务在事务外执行。
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """绑定与会话、委派、Workflow 相同的事务数据库。"""
        self.sessions = sessions

    def register(
        self, run_id: str, snapshot: dict[str, Any], *, root_run_id: str | None = None
    ) -> dict[str, Any]:
        """首次远程提交前固定权限和发布版本，绝不覆盖已有快照。"""
        with self.sessions.begin() as session:
            try:
                with session.begin_nested():
                    session.add(
                        RunExecutionRow(
                            run_id=run_id, root_run_id=root_run_id or run_id, snapshot=snapshot
                        )
                    )
                    session.flush()
            except IntegrityError:
                existing = session.get(RunExecutionRow, run_id)
                if existing is None or existing.snapshot != snapshot:
                    raise ExecutionConflict("execution snapshot cannot be replaced") from None
            return _execution(session.get(RunExecutionRow, run_id))

    def get(self, run_id: str) -> dict[str, Any]:
        """读取执行快照；旧记录没有授权证明时 fail closed。"""
        with self.sessions() as session:
            row = session.get(RunExecutionRow, run_id)
            if row is None:
                raise ExecutionConflict(
                    "execution snapshot missing; drain or reauthorize legacy run"
                )
            return _execution(row)

    def tree(self, root_run_id: str) -> tuple[dict[str, Any], ...]:
        """列出取消需要确认的整个已登记子树。"""
        with self.sessions() as session:
            return tuple(
                _execution(row)
                for row in session.scalars(
                    select(RunExecutionRow).where(RunExecutionRow.root_run_id == root_run_id)
                )
            )

    def consume(self, run_id: str, kind: str, *, session: Session | None = None) -> None:
        """每次真实尝试计入根预算；恢复、子任务与重试共用持久计数。"""
        if session is None:
            with self.sessions.begin() as transaction:
                self.consume(run_id, kind, session=transaction)
            return
        row = session.get(RunExecutionRow, run_id)
        if row is None:
            raise ExecutionConflict("execution budget is missing")
        root = session.get(RunExecutionRow, row.root_run_id)
        if root is None:
            raise ExecutionConflict("root execution budget is missing")
        column = {
            "model": RunExecutionRow.model_calls,
            "tool": RunExecutionRow.tool_calls,
            "operation": RunExecutionRow.operation_calls,
        }[kind]
        limit = root.snapshot.get("limits", {}).get(
            kind, {"model": 64, "tool": 128, "operation": 64}[kind]
        )
        # 将限额和取消条件放进 UPDATE，避免两个 worker 同时读到剩余额度
        # 后都成功扣减；rowcount 是本次是否获得额度的判断依据。
        changed = session.execute(
            update(RunExecutionRow)
            .where(
                RunExecutionRow.run_id == root.run_id,
                RunExecutionRow.cancellation_requested.is_(False),
                column < limit,
            )
            .values({column.key: column + 1}),
            execution_options={"synchronize_session": False},
        )
        if changed.rowcount != 1:
            raise ExecutionConflict(f"root {kind} budget exhausted or cancellation requested")

    def verify_context(self, context: ExecutionContext) -> None:
        """运行期复验完整身份、分类和授权上界；仅 scopes 可以进一步收窄。"""
        original = snapshot_context(self.get(context.run_id)["snapshot"])
        if original.model_dump(exclude={"scopes"}) != context.model_dump(exclude={"scopes"}) or (
            "*" not in original.scopes and not context.scopes.issubset(original.scopes)
        ):
            raise ExecutionConflict("runtime identity or scopes exceed the execution snapshot")

    def prepare(
        self,
        operation_id: str,
        run_id: str,
        request: dict[str, Any],
        *,
        session: Session | None = None,
    ) -> dict[str, Any]:
        """按业务操作键准备命令；同键不同决定或执行位置必须冲突。"""
        if session is None:
            with self.sessions.begin() as transaction:
                return self.prepare(operation_id, run_id, request, session=transaction)
        fingerprint = digest(request)
        try:
            with session.begin_nested():
                session.add(
                    RunOperationRow(
                        operation_id=operation_id,
                        run_id=run_id,
                        request_hash=fingerprint,
                        request=request,
                    )
                )
                session.flush()
        except IntegrityError:
            pass
        row = session.get(RunOperationRow, operation_id)
        if row is None or row.run_id != run_id or row.request_hash != fingerprint:
            raise ExecutionConflict("operation key reused with a different command or snapshot")
        return _operation(row)

    def claim(self, operation_id: str) -> bool:
        """只有一个进程获得提交权；领取与根预算扣减属于同一事务。"""
        with self.sessions.begin() as session:
            result = session.execute(
                update(RunOperationRow)
                .where(
                    RunOperationRow.operation_id == operation_id,
                    RunOperationRow.status == "prepared",
                )
                .values(status="claimed", updated_at=datetime.now(UTC))
            )
            if result.rowcount != 1:
                return False
            row = session.get(RunOperationRow, operation_id)
            # 扣减失败会回滚上面的 claimed，不能留下未获预算的提交权。
            self.consume(row.run_id, "operation", session=session)
            resume = (row.request.get("command") or {}).get("resume", {})
            if isinstance(resume, Mapping) and len(resume) == 1 and "decisions" not in resume:
                resume = next(iter(resume.values()))
            if isinstance(resume, Mapping) and (
                resume.get("status") == "rejected"
                or any(item.get("type") == "reject" for item in resume.get("decisions", ()))
            ):
                execution = session.get(RunExecutionRow, row.run_id)
                session.execute(
                    update(RunExecutionRow)
                    .where(
                        RunExecutionRow.run_id == execution.root_run_id,
                    )
                    .values(side_effects_denied=True)
                )
            return True

    def operation(self, operation_id: str) -> dict[str, Any]:
        """按稳定身份读取操作，不按最新记录猜测恢复目标。"""
        with self.sessions() as session:
            row = session.get(RunOperationRow, operation_id)
            if row is None:
                raise ExecutionConflict("operation not found")
            return _operation(row)

    def operations_for_run(self, run_id: str) -> tuple[dict[str, Any], ...]:
        """列出此业务运行的所有提交尝试，取消与排障不能只看最新一条。"""
        with self.sessions() as session:
            return tuple(
                _operation(row)
                for row in session.scalars(
                    select(RunOperationRow).where(RunOperationRow.run_id == run_id)
                )
            )

    def delivery_in_progress(self, run_id: str, delegation_id: str) -> bool:
        """拒绝后仅允许原委派工具接收已准备交付的终态，不把它当作新委派。

        图恢复会重新进入中断工具的 wrapper。凭工具名称放行会扩大权限，因此
        必须同时匹配原 handoff、已领取的交付操作、原生中断和确切执行链。
        新模型调用仍先受批次守卫约束，取消仍由根预算的原子检查拦截。
        """
        from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow

        with self.sessions() as session:
            execution = session.get(RunExecutionRow, run_id)
            delegation = session.get(DelegationRow, delegation_id)
            operation = session.get(
                RunOperationRow, "operation-" + digest([run_id, "delivery:" + delegation_id])
            )
            if (
                execution is None
                or delegation is None
                or operation is None
                or delegation.parent_run_id != run_id
                or delegation.execution_status not in {"completed", "rejected", "failed"}
                or operation.status not in {"claimed", "submitted", "uncertain"}
            ):
                return False
            binding = delegation.execution_snapshot or {}
            waiting = execution.waiting or {}
            predecessor = binding.get("parent_server_run_id")
            interrupt_id = binding.get("parent_interrupt_id")
            resume = (operation.request.get("command") or {}).get("resume", {})
            result = resume.get(interrupt_id, {}) if interrupt_id else resume
            return bool(
                predecessor
                and waiting.get("kind") == "handoff"
                and waiting.get("payload", {}).get("handoff_id") == delegation_id
                and waiting.get("server_run_id") == predecessor
                and operation.request.get("predecessor") == predecessor
                and execution.server_run_id in {predecessor, operation.server_run_id}
                and result.get("delegation_id") == delegation_id
            )

    def bind(self, operation_id: str, server_run_id: str) -> None:
        """幂等绑定提交回执；不同的 Server Run 不能覆盖原提交。"""
        with self.sessions.begin() as session:
            changed = session.execute(
                update(RunOperationRow)
                .where(
                    RunOperationRow.operation_id == operation_id,
                    RunOperationRow.server_run_id.is_(None),
                )
                .values(
                    server_run_id=server_run_id, status="submitted", updated_at=datetime.now(UTC)
                )
            )
            row = session.get(RunOperationRow, operation_id)
            if not changed.rowcount and row.server_run_id != server_run_id:
                raise ExecutionConflict("operation already bound to another server attempt")
            # 此时仅更新提交关联；中断与结果仍需按确切 run 对账。
            session.execute(
                update(RunExecutionRow)
                .where(
                    RunExecutionRow.run_id == row.run_id,
                )
                .values(run_id=row.run_id)
            )
            execution = session.get(RunExecutionRow, row.run_id)
            predecessor = row.request.get("predecessor")
            if execution.server_run_id in {None, predecessor, server_run_id}:
                execution.server_run_id = server_run_id

    def uncertain(self, operation_id: str) -> None:
        """回执未知时保留原操作；不能把本地异常当作远程未执行。"""
        with self.sessions.begin() as session:
            session.execute(
                update(RunOperationRow)
                .where(
                    RunOperationRow.operation_id == operation_id,
                    RunOperationRow.status == "claimed",
                )
                .values(status="uncertain", updated_at=datetime.now(UTC))
            )

    def observe(
        self,
        operation_id: str,
        result: dict[str, Any],
        *,
        delegation_id: str | None = None,
        audit: Any = None,
    ) -> None:
        """保存观察结果与交付事实；二者原子提交，崩溃后无需重发父恢复。"""
        from financeclaw.shared.audit.models import AuditEventType, AuditRecord
        from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow

        event = None
        with self.sessions.begin() as session:
            session.execute(
                update(RunOperationRow)
                .where(
                    RunOperationRow.operation_id == operation_id,
                    RunOperationRow.status != "observed",
                )
                .values(status="observed", result=result, updated_at=datetime.now(UTC))
            )
            if delegation_id is not None:
                row = session.get(DelegationRow, delegation_id)
                if (
                    row is None
                    or row.parent_run_id != session.get(RunOperationRow, operation_id).run_id
                ):
                    raise ExecutionConflict("delivery operation does not own this delegation")
                if row.delivered_at is None:
                    row.delivered_at = datetime.now(UTC)
                    row.status = "delivered"  # 旧 API 投影；execution_status 保留原终态。
                    event = AuditRecord(
                        audit_id="audit-" + digest([operation_id, "delivered"]),
                        event_type=AuditEventType.DELEGATION_DELIVERED,
                        tenant_id=row.tenant_id,
                        subject_id=row.subject_id,
                        conversation_id=row.conversation_id,
                        turn_id=row.parent_turn_id,
                        run_id=row.parent_run_id,
                        resource_type="delegation",
                        resource_id=row.delegation_id,
                        resource_version=row.target_version,
                        action="delivery",
                        decision="delivered_to_parent",
                        policy_version=row.policy_version,
                        payload_hash=row.arguments_hash,
                        metadata={"operation_id": operation_id},
                    )
                    if audit is not None and hasattr(audit, "append_in_session"):
                        audit.append_in_session(session, event)
        # 仅测试用内存审计没有数据库事务；持久化实现走上面的同事务分支。
        if event is not None and audit is not None and not hasattr(audit, "append_in_session"):
            audit.append(event)

    def set_waiting(
        self, run_id: str, waiting: dict[str, Any] | None, *, server_run_id: str | None = None
    ) -> dict[str, Any] | None:
        """保存单个中断实例；同实例不重置审批截止时间，也不允许载荷漂移。"""
        with self.sessions.begin() as session:
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.run_id == run_id)
                .values(run_id=run_id)
            )
            row = session.get(RunExecutionRow, run_id)
            if server_run_id is not None and row.server_run_id != server_run_id:
                raise ExecutionConflict(
                    "stale observation of a previous server attempt; query again"
                )
            if row.waiting and waiting and row.waiting["key"] == waiting["key"]:
                if row.waiting["payload_hash"] != waiting["payload_hash"]:
                    raise ExecutionConflict("pending action changed without a new interrupt")
                return row.waiting
            row.waiting = waiting
            return waiting

    def request_cancel(self, root_run_id: str) -> None:
        """先关闭整个任务树的新派发；底层停止确认由应用服务另行处理。"""
        with self.sessions.begin() as session:
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.root_run_id == root_run_id)
                .values(cancellation_requested=True)
            )

    def confirm_cancel(self, run_id: str) -> None:
        """仅在精确执行尝试已停止后记录底层取消确认。"""
        with self.sessions.begin() as session:
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.run_id == run_id)
                .values(cancellation_confirmed=True)
            )

    def deny_side_effects(self, run_id: str) -> None:
        """拒绝后禁止根任务树再派发写动作或新委派，不能换工具绕过。"""
        with self.sessions.begin() as session:
            row = session.get(RunExecutionRow, run_id)
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.run_id == row.root_run_id)
                .values(side_effects_denied=True)
            )
