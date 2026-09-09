"""跨进程执行事实仓储：CAS 领取、单调回执、持久预算与恢复授权。"""

import json
from collections.abc import Mapping
from contextlib import nullcontext
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


def _ensure_savepoint_transaction(session: Session) -> None:
    """SQLite legacy 模式首次 SAVEPOINT 前显式 BEGIN，避免 RELEASE 提前提交。

    Session 仍唯一拥有外层 commit／rollback。PostgreSQL 已有真实事务，不需补发。
    """
    connection = session.connection()
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.dbapi_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN")


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
        """绑定与会话、子图调用、Workflow 相同的事务数据库。"""
        self.sessions = sessions

    def register(
        self,
        run_id: str,
        snapshot: dict[str, Any],
        *,
        session: Session | None = None,
    ) -> dict[str, Any]:
        """固定权限、发布和根归属；显式 Session 参与受理事务且不自行提交。"""
        if session is None:
            with self.sessions.begin() as transaction:
                return self.register(run_id, snapshot, session=transaction)
        _ensure_savepoint_transaction(session)
        try:
            with session.begin_nested():
                session.add(RunExecutionRow(run_id=run_id, root_run_id=run_id, snapshot=snapshot))
                session.flush()
        except IntegrityError:
            existing = session.get(RunExecutionRow, run_id)
            if existing is None or existing.snapshot != snapshot or existing.root_run_id != run_id:
                raise ExecutionConflict("execution snapshot or root cannot be replaced") from None
        return _execution(session.get(RunExecutionRow, run_id))

    def get(self, run_id: str) -> dict[str, Any]:
        """读取执行快照；旧记录没有授权证明时 fail closed。"""
        with self.sessions() as session:
            row = session.get(RunExecutionRow, run_id)
            if row is None:
                raise ExecutionConflict("execution snapshot is missing")
            return _execution(row)

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
        if root.snapshot.get("profile", {}).get("worker_manifest"):
            from financeclaw.shared.execution_ledger.authorization import check_authorization

            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.run_id == root.run_id)
                .values(run_id=root.run_id)
            )
            check_authorization(session, root)
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
        with self.sessions() as session:
            from financeclaw.shared.execution_ledger.authorization import check_authorization

            root = session.get(RunExecutionRow, context.root_run_id or context.run_id)
            check_authorization(session, root, scopes=context.scopes)

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
        _ensure_savepoint_transaction(session)
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

    def claim(self, operation_id: str, *, session: Session | None = None) -> bool:
        """只有一个进程获得提交权；领取与根预算扣减属于同一事务。"""
        if session is None:
            with self.sessions.begin() as transaction:
                return self.claim(operation_id, session=transaction)
        return self._claim_in_session(operation_id, session)

    def _claim_in_session(self, operation_id: str, session: Session) -> bool:
        """在调用方的授权、租约和预算事务中唯一领取，不打开第二个连接。"""
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

    def observe_in_session(
        self,
        session: Session,
        operation_id: str,
        *,
        server_run_id: str,
        result: dict[str, Any],
    ) -> None:
        """完成事务的确切尝试证据；Journal／投影失败时与观察结果一同回滚。"""
        row = session.scalar(
            select(RunOperationRow)
            .where(RunOperationRow.operation_id == operation_id)
            .with_for_update()
        )
        if (
            row is None
            or row.server_run_id != server_run_id
            or row.status not in {"submitted", "observed"}
        ):
            raise ExecutionConflict("terminal evidence does not match the submitted operation")
        if row.status == "observed" and row.result != result:
            raise ExecutionConflict("terminal evidence cannot be replaced")
        row.status = "observed"
        row.result = result
        row.updated_at = datetime.now(UTC)

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

    def bind(
        self, operation_id: str, server_run_id: str, *, session: Session | None = None
    ) -> None:
        """幂等绑定提交回执；不同的 Server Run 不能覆盖原提交。"""
        with nullcontext(session) if session is not None else self.sessions.begin() as session:
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

    def uncertain(self, operation_id: str, *, session: Session | None = None) -> None:
        """回执未知时保留原操作；不能把本地异常当作远程未执行。"""
        with nullcontext(session) if session is not None else self.sessions.begin() as session:
            session.execute(
                update(RunOperationRow)
                .where(
                    RunOperationRow.operation_id == operation_id,
                    RunOperationRow.status == "claimed",
                )
                .values(status="uncertain", updated_at=datetime.now(UTC))
            )

    def request_cancel(self, root_run_id: str, *, session: Session | None = None) -> None:
        """先关闭根执行的新派发；底层停止确认由应用服务另行处理。"""
        with nullcontext(session) if session is not None else self.sessions.begin() as session:
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.root_run_id == root_run_id)
                .values(cancellation_requested=True)
            )

    def deny_side_effects(self, run_id: str) -> None:
        """拒绝后禁止根执行再派发写动作或新子图调用，不能换工具绕过。"""
        with self.sessions.begin() as session:
            row = session.get(RunExecutionRow, run_id)
            session.execute(
                update(RunExecutionRow)
                .where(RunExecutionRow.run_id == row.root_run_id)
                .values(side_effects_denied=True)
            )
