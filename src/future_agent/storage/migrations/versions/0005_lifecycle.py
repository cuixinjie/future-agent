"""Append-only T10 seals, resolutions and durable forecast steps."""

import sqlalchemy as sa
from alembic import op

revision = "0005_lifecycle"
down_revision = "0004_forecasts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "frozen_forecasts",
        sa.Column("forecast_id", sa.String(36), sa.ForeignKey("forecasts.id"), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "resolutions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "forecast_id",
            sa.String(36),
            sa.ForeignKey("frozen_forecasts.forecast_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("forecast_id", "sequence"),
    )
    op.create_table(
        "forecast_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id", sa.String(36), sa.ForeignKey("analysis_sessions.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("冻结核验迁移不可无损回退；请恢复升级前完整数据目录备份")
