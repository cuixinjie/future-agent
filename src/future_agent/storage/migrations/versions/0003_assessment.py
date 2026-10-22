"""Add immutable T08 assessments without rewriting previous contracts."""

import sqlalchemy as sa
from alembic import op

revision = "0003_assessment"
down_revision = "0002_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_assessments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_revision_id", sa.String(36), sa.ForeignKey("event_revisions.id"), nullable=False
        ),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_evidence_assessments_event_revision_id", "evidence_assessments", ["event_revision_id"]
    )


def downgrade() -> None:
    raise RuntimeError("评估迁移不可无损回退；请恢复升级前数据库及快照目录备份")
