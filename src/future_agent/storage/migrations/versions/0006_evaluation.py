"""Durable complete A/B/C registry and immutable input/first-prediction snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0006_evaluation"
down_revision = "0005_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(36), sa.ForeignKey("evaluation_protocols.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("实验迁移不可无损回退；请恢复升级前完整数据目录备份")
