"""Stage-8C 部署门闩与旧根原始证据；不在建表或升级时接管任务。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.shared.infrastructure.orm import Base, utcnow


class CoordinationControlRow(Base):
    """同库所有角色共用的部署门闩；封闭旧驱动后不能重新开放。"""

    __tablename__ = "coordination_control"
    control_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    admission_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    legacy_fenced: Mapped[bool] = mapped_column(Boolean, default=False)
    stopped_evidence_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LegacyAdoptionRow(Base):
    """接管的原始行、影子观察摘要与 CAS 依据；只追加，禁止丢弃历史证据。"""

    __tablename__ = "legacy_adoptions"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    shadow_hash: Mapped[str] = mapped_column(String(64))
    control_revision: Mapped[int] = mapped_column(Integer)
    original: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def seed_control(_target, connection, **_kwargs):
    """开发建表与正式 Alembic 使用相同初始门闩，旧根仍无人接管。"""
    connection.execute(CoordinationControlRow.__table__.insert().values(control_id=1))


event.listen(CoordinationControlRow.__table__, "after_create", seed_control)
