"""Stage-8C deployment fences and immutable legacy adoption evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0011_stage8c"
down_revision = "0010_stage8b"
branch_labels = None
depends_on = None


def upgrade():
    """仅扩表并创建未封闭门闩，不访问 backend、不接管或补发旧任务。"""
    op.create_table(
        "coordination_control",
        sa.Column("control_id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("admission_paused", sa.Boolean(), nullable=False),
        sa.Column("dispatch_paused", sa.Boolean(), nullable=False),
        sa.Column("legacy_fenced", sa.Boolean(), nullable=False),
        sa.Column("stopped_evidence_hash", sa.String(64)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    control = sa.table(
        "coordination_control",
        sa.column("control_id", sa.Integer()),
        sa.column("revision", sa.Integer()),
        sa.column("admission_paused", sa.Boolean()),
        sa.column("dispatch_paused", sa.Boolean()),
        sa.column("legacy_fenced", sa.Boolean()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    from datetime import UTC, datetime

    op.bulk_insert(
        control,
        [
            {
                "control_id": 1,
                "revision": 0,
                "admission_paused": False,
                "dispatch_paused": False,
                "legacy_fenced": False,
                "updated_at": datetime.now(UTC),
            }
        ],
    )
    op.create_table(
        "legacy_adoptions",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("shadow_hash", sa.String(64), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("original", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    """切换过或存在接管事实时拒绝降级；回滚应保留兼容角色与数据库。"""
    connection = op.get_bind()
    if (
        connection.execute(sa.text("SELECT 1 FROM legacy_adoptions LIMIT 1")).first()
        or (
            connection.execute(
                sa.text("SELECT 1 FROM coordination_control WHERE revision > 0")
            ).first()
        )
        or connection.execute(
            sa.text("SELECT 1 FROM coordinated_runs WHERE driver_version >= 3 LIMIT 1")
        ).first()
    ):
        raise RuntimeError("Stage-8C cutover evidence must be retained")
    op.drop_table("legacy_adoptions")
    op.drop_table("coordination_control")
