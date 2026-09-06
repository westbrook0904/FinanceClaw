"""业务执行快照和出站操作日志，记录已发生的事实而不是第二套运行时。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class RunExecutionRow(Base):
    """每个业务 run 的不可变执行快照和当前执行尝试关联。

    snapshot 内保存原始权限上界与实际发布版本。root_run_id 指向预算归属；
    waiting 仅保存 A 阶段已有的单中断信息，不是 C 阶段通用交互表。
    """

    __tablename__ = "run_executions"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    server_run_id: Mapped[str | None] = mapped_column(String(128))
    waiting: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancellation_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    side_effects_denied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operation_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunOperationRow(Base):
    """一次 start/resume 的稳定身份、原子领取及服务端回执。

    claimed 没有可自动重领的超时：进程崩溃后无法证明命令未提交，必须对账；
    无法对账就保持 uncertain。该保守策略不声称远程 exactly-once。
    """

    __tablename__ = "run_operations"

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run_executions.run_id"), index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="prepared", nullable=False)
    server_run_id: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
