"""Add T09 drafts; preserve all previous event/evidence/assessment records."""

import sqlalchemy as sa
from alembic import op

revision = "0004_forecasts"
down_revision = "0003_assessment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecasts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_revision_id", sa.String(36), sa.ForeignKey("event_revisions.id"), nullable=False
        ),
        sa.Column(
            "assessment_id", sa.String(36), sa.ForeignKey("evidence_assessments.id"), nullable=False
        ),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_forecasts_event_revision_id", "forecasts", ["event_revision_id"])


def downgrade() -> None:
    raise RuntimeError("预测迁移不可无损回退；请恢复升级前数据库及快照目录备份")
