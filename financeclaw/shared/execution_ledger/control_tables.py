"""BFF 受理与后台调度开关。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, event
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base, utcnow


class RunControlRow(Base):
    """共享受理和调度开关，revision 防止并发配置覆盖。"""

    __tablename__ = "run_control"
    control_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    bff_admission_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    bff_dispatch_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def seed_control(_target, connection, **_kwargs):
    """开发建表与 Alembic 使用相同初始门闩。"""
    connection.execute(RunControlRow.__table__.insert().values(control_id=1))


event.listen(RunControlRow.__table__, "after_create", seed_control)
