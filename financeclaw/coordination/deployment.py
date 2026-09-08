"""可审查的部署 CAS、唯一驱动封闭与无敏感载荷的运维诊断。"""

import re
from datetime import timedelta

from sqlalchemy import func, or_, select, text, update

from financeclaw.coordination.repository import DRIVER_VERSION, aware, now
from financeclaw.shared.execution_ledger.coordination_tables import (
    CoordinatedRunRow,
    CoordinationInboxRow,
    CoordinatorHeartbeatRow,
    RunAuthorizationRow,
)
from financeclaw.shared.execution_ledger.cutover_tables import CoordinationControlRow
from financeclaw.shared.execution_ledger.delegation_tables import DelegationRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow
from financeclaw.shared.notifications.tables import (
    NotificationDeliveryRow,
    NotificationEventRow,
    NotificationTargetRow,
)


def control_view(row):
    """输出部署开关和证据摘要，绝不输出证明文件的凭证或环境配置。"""
    return {
        key: getattr(row, key)
        for key in (
            "revision",
            "admission_paused",
            "dispatch_paused",
            "legacy_fenced",
            "stopped_evidence_hash",
        )
    }


class DeploymentControl:
    """操作员先停止旧生产者并保存证明，数据库再原子封闭旧版本和驱动。"""

    def __init__(self, store):
        """使用 Coordinator 的同库 Session 与 backend 绑定。"""
        self.store = store

    def view(self):
        """只读当前部署 revision，供下一条 CAS 命令明确引用。"""
        with self.store.sessions() as session:
            return control_view(session.get(CoordinationControlRow, 1))

    def change(self, revision, *, admission_paused, dispatch_paused, stopped_evidence_hash=None):
        """暂停／恢复只能按原 revision；封闭旧驱动不可逆，绝不降回 legacy。"""
        with self.store.sessions.begin() as session:
            row = session.scalar(
                select(CoordinationControlRow)
                .where(CoordinationControlRow.control_id == 1)
                .with_for_update()
            )
            if row is None or row.revision != revision:
                raise ExecutionConflict("deployment revision changed")
            if stopped_evidence_hash is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", stopped_evidence_hash):
                    raise ExecutionConflict("a stopped-producer evidence SHA-256 is required")
                if not row.admission_paused or not row.dispatch_paused:
                    raise ExecutionConflict("pause admission and dispatch before sealing drivers")
                if not admission_paused or not dispatch_paused or row.legacy_fenced:
                    raise ExecutionConflict("seal exactly once while both gates remain paused")
                row.legacy_fenced, row.stopped_evidence_hash = True, stopped_evidence_hash
                # 必须先停旧 Worker；升版本防止其重启，epoch 防止迟到事务写回。
                session.execute(
                    update(CoordinatedRunRow)
                    .where(
                        CoordinatedRunRow.driver_version.in_((1, 2)),
                    )
                    .values(
                        driver_version=DRIVER_VERSION,
                        epoch=CoordinatedRunRow.epoch + 1,
                        owner=None,
                        lease_until=None,
                    )
                )
            row.admission_paused, row.dispatch_paused = admission_paused, dispatch_paused
            row.revision, row.updated_at = row.revision + 1, now()
            return control_view(row)


def diagnostics(store):
    """按 backend 聚合计数和最老年龄；任务与租户 ID 不成为指标维度。"""
    current = now()

    def age(value):
        """未知时间保持 null，避免把未取得的观测伪报为零。"""
        return max(0, (current - aware(value)).total_seconds()) if value else None

    with store.sessions() as session:
        backend = store.backend_instance_id
        roots = select(CoordinatedRunRow.run_id).where(
            CoordinatedRunRow.backend_instance_id == backend,
        )
        due = session.execute(
            select(func.count(), func.min(CoordinatedRunRow.due_at)).where(
                CoordinatedRunRow.backend_instance_id == backend,
                CoordinatedRunRow.active.is_(True),
                CoordinatedRunRow.due_at <= current,
            )
        ).one()
        unknown = session.execute(
            select(func.count(), func.min(RunOperationRow.updated_at))
            .select_from(RunOperationRow)
            .join(RunExecutionRow)
            .where(
                RunExecutionRow.root_run_id.in_(roots),
                RunOperationRow.status.in_(("claimed", "uncertain")),
                RunOperationRow.server_run_id.is_(None),
            )
        ).one()
        inbox = session.execute(
            select(func.count(), func.min(CoordinationInboxRow.received_at)).where(
                CoordinationInboxRow.backend_instance_id == backend,
                CoordinationInboxRow.processed.is_(False),
            )
        ).one()
        workers = session.scalar(
            select(func.count())
            .select_from(CoordinatorHeartbeatRow)
            .where(
                CoordinatorHeartbeatRow.backend_instance_id == backend,
                CoordinatorHeartbeatRow.driver_version == DRIVER_VERSION,
                CoordinatorHeartbeatRow.heartbeat_at > current - timedelta(seconds=10),
            )
        )
        active = session.scalar(
            select(func.count())
            .select_from(CoordinatedRunRow)
            .where(
                CoordinatedRunRow.backend_instance_id == backend,
                CoordinatedRunRow.active.is_(True),
            )
        )
        grants = session.scalar(
            select(func.count())
            .select_from(RunAuthorizationRow)
            .where(
                RunAuthorizationRow.run_id.in_(roots),
                RunAuthorizationRow.run_id.in_(roots.where(CoordinatedRunRow.active.is_(True))),
                or_(
                    RunAuthorizationRow.expires_at <= current, RunAuthorizationRow.revoked.is_(True)
                ),
            )
        )
        waiting = session.scalar(
            select(func.count())
            .select_from(PendingInteractionRow)
            .where(
                PendingInteractionRow.root_run_id.in_(roots),
                PendingInteractionRow.status == "pending",
            )
        )
        unbound = session.execute(
            select(func.count(), func.min(CoordinationInboxRow.received_at)).where(
                CoordinationInboxRow.backend_instance_id == backend,
                CoordinationInboxRow.run_id.is_(None),
                CoordinationInboxRow.processed.is_(False),
            )
        ).one()
        delivery = session.execute(
            select(func.count(), func.min(DelegationRow.completed_at)).where(
                DelegationRow.parent_run_id.in_(roots),
                DelegationRow.completed_at.is_not(None),
                DelegationRow.delivered_at.is_(None),
            )
        ).one()
        cancelling = session.scalar(
            select(func.count())
            .select_from(RunExecutionRow)
            .where(
                RunExecutionRow.run_id.in_(roots),
                RunExecutionRow.cancellation_requested.is_(True),
                RunExecutionRow.cancellation_confirmed.is_(False),
            )
        )
        notifications = dict(
            session.execute(
                select(NotificationDeliveryRow.status, func.count())
                .join(NotificationEventRow)
                .join(NotificationTargetRow)
                .where(NotificationTargetRow.run_id.in_(roots))
                .group_by(NotificationDeliveryRow.status)
            ).all()
        )
        lock_waiters = None
        if session.get_bind().dialect.name == "postgresql":
            lock_waiters = session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        pool = session.get_bind().pool
        return {
            "control": control_view(session.get(CoordinationControlRow, 1)),
            "driver_version": DRIVER_VERSION,
            "backend_instance_id": backend,
            "compatible_workers": workers,
            "active_roots": active,
            "due_roots": due[0],
            "oldest_due_seconds": age(due[1]),
            "uncertain_operations": unknown[0],
            "oldest_uncertain_seconds": age(unknown[1]),
            "inbox_pending": inbox[0],
            "oldest_inbox_seconds": age(inbox[1]),
            "authorization_required": grants,
            "pending_interactions": waiting,
            "unmatched_inbox": unbound[0],
            "oldest_unmatched_inbox_seconds": age(unbound[1]),
            "child_results_pending_delivery": delivery[0],
            "oldest_child_delivery_seconds": age(delivery[1]),
            "cancellations_unconfirmed": cancelling,
            "notification_deliveries_by_status": notifications,
            "database_lock_waiters": lock_waiters,
            "local_pool_checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None,
            "backend_readiness": "not_probed",
            "notification_readiness": "separate_sender",
        }
