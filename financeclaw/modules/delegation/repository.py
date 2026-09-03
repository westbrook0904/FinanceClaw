"""委派记录的持久化仓库：Protocol 接口与 SQLAlchemy 实现。

``delegations`` 表是委派生命周期的事实来源：本模块负责幂等受理（REQUESTED）、
child 身份准备与绑定、状态机推进（含终态保护与交付标记），以及面向父运行与
child 运行的租户隔离查询。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import DelegationKind, DelegationRecord, DelegationStatus
from .tables import DelegationRow


class DelegationNotFound(LookupError):
    """按给定条件找不到委派记录时抛出的查询异常。"""

    pass


class DelegationConflict(RuntimeError):
    """委派状态或身份冲突时抛出的运行时异常。

    典型场景包括：同一 handoff ID 被复用于不同请求、委派已绑定到其他 child
    run，以及已进入终态的委派被再次改写状态。
    """

    pass


class DelegationRepository(Protocol):
    """委派仓库接口，抽象 ``delegations`` 表的全部读写操作。

    使用场景：应用层 DelegationService 依赖该协议完成受理、启动、状态同步与
    结果交付；生产环境使用 SqlAlchemyDelegationRepository，测试可替换为内存
    实现。
    """

    def ensure_requested(
        self,
        *,
        delegation_id: str,
        tenant_id: str,
        subject_id: str,
        conversation_id: str,
        parent_turn_id: str,
        parent_run_id: str,
        kind: DelegationKind,
        target_id: str,
        target_version: str,
        arguments: dict[str, Any],
    ) -> tuple[DelegationRecord, bool]:
        """幂等受理委派请求：已存在则校验一致性，否则新建 REQUESTED 记录。

        Args:
            delegation_id: 委派唯一标识（即 handoff ID），作记录主键。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。
            conversation_id: 所属会话 ID。
            parent_turn_id: 父会话轮次 ID。
            parent_run_id: 父 Agent 运行 ID。
            kind: 委派种类。
            target_id: 目标标识。
            target_version: 目标版本号。
            arguments: 委派参数字典。

        Returns:
            ``(委派记录, 是否新建)`` 二元组；记录已存在时返回既有记录与 False。

        Raises:
            DelegationConflict: 同一 handoff ID 被用于不同租户、主体或请求
                指纹的委派请求。

        """
        ...

    def prepare_agent_child(self, delegation_id: str) -> DelegationRecord:
        """为 Agent 委派准备 child 运行身份并把状态推进到 PENDING。

        Args:
            delegation_id: 委派唯一标识。

        Returns:
            已分配 child_run_id 与 child_thread_id 的最新委派记录。

        Raises:
            DelegationNotFound: 委派记录不存在。
            DelegationConflict: 非 Agent 类委派不允许自备 child run。

        """
        ...

    def bind_child(
        self,
        delegation_id: str,
        *,
        child_run_id: str,
        child_thread_id: str,
        child_server_run_id: str | None,
        status: DelegationStatus,
    ) -> DelegationRecord:
        """把 child run/thread（及可选 server run）绑定到委派并更新状态。

        Args:
            delegation_id: 委派唯一标识。
            child_run_id: child 运行 ID。
            child_thread_id: child 的 thread ID。
            child_server_run_id: agent server 侧运行 ID，可为 None 后补。
            status: 绑定后要写入的委派状态（如 RUNNING 或 INTERRUPTED）。

        Returns:
            绑定完成后的最新委派记录。

        Raises:
            DelegationNotFound: 委派记录不存在。
            DelegationConflict: 委派已绑定到其他 child run、thread 或
                server run。

        """
        ...

    def set_status(
        self,
        delegation_id: str,
        status: DelegationStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> tuple[DelegationRecord, bool]:
        """推进委派状态，保护终态不被改写并按需记录结果与时间戳。

        Args:
            delegation_id: 委派唯一标识。
            status: 目标状态。
            output_payload: 终态输出载荷，None 表示不改写既有值。
            error: 失败或被拒原因，None 表示不改写既有值。

        Returns:
            ``(最新记录, 状态是否发生变化)`` 二元组；状态不变时返回 False。

        Raises:
            DelegationNotFound: 委派记录不存在。
            DelegationConflict: 已进入完成、拒绝或失败终态后再次改变状态
                （交付标记 DELIVERED 除外）。

        """
        ...

    def get_owned(self, delegation_id: str, tenant_id: str, subject_id: str) -> DelegationRecord:
        """按租户与主体归属读取指定委派记录。

        Args:
            delegation_id: 委派唯一标识。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            归属校验通过的委派记录。

        Raises:
            DelegationNotFound: 记录不存在或不属于该租户与主体。

        """
        ...

    def get_by_child_owned(
        self, child_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord:
        """按 child 运行 ID 与租户主体归属反查委派记录。

        Args:
            child_run_id: child 运行 ID。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            归属校验通过的委派记录。

        Raises:
            DelegationNotFound: 记录不存在或不属于该租户与主体。

        """
        ...

    def latest_undelivered_for_parent(
        self, parent_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord | None:
        """查询某父运行下最近一条尚未交付（非 DELIVERED）的委派记录。

        Args:
            parent_run_id: 父 Agent 运行 ID。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            按创建时间最新的未交付记录；没有时返回 None。

        """
        ...

    def list_undelivered(self) -> tuple[DelegationRecord, ...]:
        """列出全部未交付的委派记录，按创建时间升序返回。"""
        ...


def _record(row: DelegationRow) -> DelegationRecord:
    """把 ORM 行转换为不可变的 DelegationRecord 领域模型。"""
    return DelegationRecord(
        delegation_id=row.delegation_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        conversation_id=row.conversation_id,
        parent_turn_id=row.parent_turn_id,
        parent_run_id=row.parent_run_id,
        kind=DelegationKind(row.kind),
        target_id=row.target_id,
        target_version=row.target_version,
        arguments=row.arguments,
        arguments_hash=row.arguments_hash,
        request_fingerprint=row.request_fingerprint,
        authorization_decision=row.authorization_decision,
        policy_version=row.policy_version,
        child_run_id=row.child_run_id,
        child_thread_id=row.child_thread_id,
        child_server_run_id=row.child_server_run_id,
        status=DelegationStatus(row.status),
        output_payload=row.output_payload,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        delivered_at=row.delivered_at,
    )


class SqlAlchemyDelegationRepository:
    """基于 SQLAlchemy 的委派仓库实现，每个写方法都在独立事务中原子完成。

    使用场景：生产环境注入 sessionmaker 后供 DelegationService 使用；读方法
    使用普通会话，写方法使用 ``sessions.begin()``，异常时整体回滚。
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """初始化仓库。

        Args:
            sessions: 指向业务库的 SQLAlchemy sessionmaker 工厂。

        """
        self._sessions = sessions

    def ensure_requested(
        self,
        *,
        delegation_id: str,
        tenant_id: str,
        subject_id: str,
        conversation_id: str,
        parent_turn_id: str,
        parent_run_id: str,
        kind: DelegationKind,
        target_id: str,
        target_version: str,
        arguments: dict[str, Any],
    ) -> tuple[DelegationRecord, bool]:
        """幂等受理委派请求，返回记录与是否新建的标记。

        Args:
            delegation_id: 委派唯一标识（即 handoff ID），作记录主键。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。
            conversation_id: 所属会话 ID。
            parent_turn_id: 父会话轮次 ID。
            parent_run_id: 父 Agent 运行 ID。
            kind: 委派种类。
            target_id: 目标标识。
            target_version: 目标版本号。
            arguments: 委派参数字典。

        Returns:
            ``(委派记录, 是否新建)`` 二元组；重复受理同一请求时返回
            ``(既有记录, False)``。

        Raises:
            DelegationConflict: 同一 handoff ID 被用于不同租户、主体或请求
                指纹的委派请求。

        """
        # 1. 计算参数摘要与请求指纹，指纹覆盖会话、父子运行、目标与参数。
        arguments_hash = _hash(arguments)
        fingerprint = _hash(
            {
                "conversation_id": conversation_id,
                "parent_turn_id": parent_turn_id,
                "parent_run_id": parent_run_id,
                "kind": kind.value,
                "target_id": target_id,
                "target_version": target_version,
                "arguments": arguments,
            }
        )
        with self._sessions.begin() as session:
            # 2. 检查 handoff ID 是否已被占用。
            existing = session.get(DelegationRow, delegation_id)
            if existing is not None:
                # 3. 已存在：租户、主体与指纹必须完全一致，否则判定为复用冲突。
                if (
                    existing.tenant_id != tenant_id
                    or existing.subject_id != subject_id
                    or existing.request_fingerprint != fingerprint
                ):
                    raise DelegationConflict("handoff ID was reused for another delegation")
                return _record(existing), False
            # 4. 不存在：以 REQUESTED 状态新建记录并提交事务。
            now = datetime.now(UTC)
            row = DelegationRow(
                delegation_id=delegation_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                conversation_id=conversation_id,
                parent_turn_id=parent_turn_id,
                parent_run_id=parent_run_id,
                kind=kind.value,
                target_id=target_id,
                target_version=target_version,
                arguments=arguments,
                arguments_hash=arguments_hash,
                request_fingerprint=fingerprint,
                authorization_decision="allowed",
                policy_version="delegation-policy/1.0.0",
                status=DelegationStatus.REQUESTED.value,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        return _record(row), True

    def prepare_agent_child(self, delegation_id: str) -> DelegationRecord:
        """为 Agent 委派生成本地 child 身份并把状态推进到 PENDING。

        Args:
            delegation_id: 委派唯一标识。

        Returns:
            已分配 child_run_id 与 child_thread_id 的最新委派记录。

        Raises:
            DelegationNotFound: 委派记录不存在。
            DelegationConflict: 非 Agent 类委派不允许自备 child run。

        """
        with self._sessions.begin() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise DelegationNotFound("delegation was not found")
            # 1. 只有 Agent 委派需要自行准备 child 身份（Workflow 由启动结果回填）。
            if row.kind != DelegationKind.AGENT.value:
                raise DelegationConflict("only Agent delegation prepares its own child run")
            # 2. 首次调用时生成 child run 与 thread 标识并置为 PENDING；重复调用幂等。
            if row.child_run_id is None:
                row.child_run_id = f"run-{uuid4().hex}"
                row.child_thread_id = str(uuid4())
                row.status = DelegationStatus.PENDING.value
                row.updated_at = datetime.now(UTC)
        return _record(row)

    def bind_child(
        self,
        delegation_id: str,
        *,
        child_run_id: str,
        child_thread_id: str,
        child_server_run_id: str | None,
        status: DelegationStatus,
    ) -> DelegationRecord:
        """把 child run/thread（及可选 server run）绑定到委派并更新状态。

        Args:
            delegation_id: 委派唯一标识。
            child_run_id: child 运行 ID。
            child_thread_id: child 的 thread ID。
            child_server_run_id: agent server 侧运行 ID，可为 None 后补。
            status: 绑定后要写入的委派状态（如 RUNNING 或 INTERRUPTED）。

        Returns:
            绑定完成后的最新委派记录。

        Raises:
            DelegationNotFound: 委派记录不存在。
            DelegationConflict: 委派已绑定到其他 child run、thread 或
                server run。

        """
        with self._sessions.begin() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise DelegationNotFound("delegation was not found")
            # 1. 逐项校验 child run、thread 与 server run 未被绑定到其他值。
            if row.child_run_id is not None and row.child_run_id != child_run_id:
                raise DelegationConflict("delegation is already bound to another child run")
            if row.child_thread_id is not None and row.child_thread_id != child_thread_id:
                raise DelegationConflict("delegation is already bound to another child thread")
            if (
                row.child_server_run_id is not None
                and child_server_run_id is not None
                and row.child_server_run_id != child_server_run_id
            ):
                raise DelegationConflict("delegation is already bound to another server run")
            # 2. 写入绑定信息与目标状态；server run 允许为 None 留待后补。
            row.child_run_id = child_run_id
            row.child_thread_id = child_thread_id
            if child_server_run_id is not None:
                row.child_server_run_id = child_server_run_id
            row.status = status.value
            row.updated_at = datetime.now(UTC)
        return _record(row)

    def set_status(
        self,
        delegation_id: str,
        status: DelegationStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> tuple[DelegationRecord, bool]:
        """推进委派状态并按需记录结果、错误与时间戳。

        Args:
            delegation_id: 委派唯一标识。
            status: 目标状态。
            output_payload: 终态输出载荷，None 表示不改写既有值。
            error: 失败或被拒原因，None 表示不改写既有值。

        Returns:
            ``(最新记录, 状态是否发生变化)`` 二元组。

        Raises:
            DelegationNotFound: 委派记录不存在。
            DelegationConflict: 已进入完成、拒绝或失败终态后再次改变状态
                （交付标记 DELIVERED 除外）。

        """
        with self._sessions.begin() as session:
            row = session.get(DelegationRow, delegation_id)
            if row is None:
                raise DelegationNotFound("delegation was not found")
            current = DelegationStatus(row.status)
            terminal = {
                DelegationStatus.COMPLETED,
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
                DelegationStatus.DELIVERED,
            }
            # 1. 终态保护：完成类终态仅允许追加 DELIVERED；重复设置同状态视为幂等。
            if current in terminal and not (
                current is not DelegationStatus.DELIVERED and status is DelegationStatus.DELIVERED
            ):
                if current is status:
                    return _record(row), False
                raise DelegationConflict("terminal delegation status cannot be changed")
            # 2. 写入新状态与可选的输出载荷、错误信息。
            changed = current is not status
            now = datetime.now(UTC)
            row.status = status.value
            row.updated_at = now
            if output_payload is not None:
                row.output_payload = output_payload
            if error is not None:
                row.error = error
            # 3. 首次进入完成类终态或交付态时补记对应时间戳。
            if status in {
                DelegationStatus.COMPLETED,
                DelegationStatus.REJECTED,
                DelegationStatus.FAILED,
            }:
                row.completed_at = row.completed_at or now
            if status is DelegationStatus.DELIVERED:
                row.delivered_at = row.delivered_at or now
        return _record(row), changed

    def get_owned(self, delegation_id: str, tenant_id: str, subject_id: str) -> DelegationRecord:
        """按租户与主体归属读取指定委派记录。

        Args:
            delegation_id: 委派唯一标识。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            归属校验通过的委派记录。

        Raises:
            DelegationNotFound: 记录不存在或不属于该租户与主体。

        """
        statement = select(DelegationRow).where(
            DelegationRow.delegation_id == delegation_id,
            DelegationRow.tenant_id == tenant_id,
            DelegationRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise DelegationNotFound("delegation was not found for authenticated owner")
            return _record(row)

    def get_by_child_owned(
        self, child_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord:
        """按 child 运行 ID 与租户主体归属反查委派记录。

        Args:
            child_run_id: child 运行 ID。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            归属校验通过的委派记录。

        Raises:
            DelegationNotFound: 记录不存在或不属于该租户与主体。

        """
        statement = select(DelegationRow).where(
            DelegationRow.child_run_id == child_run_id,
            DelegationRow.tenant_id == tenant_id,
            DelegationRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise DelegationNotFound(
                    "delegated child run was not found for authenticated owner"
                )
            return _record(row)

    def latest_undelivered_for_parent(
        self, parent_run_id: str, tenant_id: str, subject_id: str
    ) -> DelegationRecord | None:
        """查询某父运行下最近一条尚未交付的委派记录。

        Args:
            parent_run_id: 父 Agent 运行 ID。
            tenant_id: 租户 ID。
            subject_id: 主体（用户）ID。

        Returns:
            按创建时间最新的未交付记录；没有时返回 None。

        """
        statement = (
            select(DelegationRow)
            .where(
                DelegationRow.parent_run_id == parent_run_id,
                DelegationRow.tenant_id == tenant_id,
                DelegationRow.subject_id == subject_id,
                DelegationRow.status != DelegationStatus.DELIVERED.value,
            )
            .order_by(DelegationRow.created_at.desc())
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            return _record(row) if row is not None else None

    def list_undelivered(self) -> tuple[DelegationRecord, ...]:
        """列出全部未交付的委派记录，按创建时间升序返回。"""
        statement = (
            select(DelegationRow)
            .where(DelegationRow.status != DelegationStatus.DELIVERED.value)
            .order_by(DelegationRow.created_at)
        )
        with self._sessions() as session:
            return tuple(_record(row) for row in session.scalars(statement))


def _hash(value: dict[str, Any]) -> str:
    """对字典做键排序的紧凑 JSON 序列化后计算 SHA-256 十六进制摘要。"""
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
