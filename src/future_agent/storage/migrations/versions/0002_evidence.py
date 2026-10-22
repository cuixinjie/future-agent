"""Add evidence metadata and citations without modifying M1 records."""

import sqlalchemy as sa
from alembic import op

revision = "0002_evidence"
down_revision = "0001_m1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_revision_id", sa.String(36), sa.ForeignKey("event_revisions.id"), nullable=False
        ),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_evidence_documents_event_revision_id", "evidence_documents", ["event_revision_id"]
    )
    op.create_table(
        "evidence_spans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "document_id", sa.String(36), sa.ForeignKey("evidence_documents.id"), nullable=False
        ),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_evidence_spans_document_id", "evidence_spans", ["document_id"])


def downgrade() -> None:
    raise RuntimeError("证据迁移不可无损回退；请恢复升级前数据库及快照目录备份")
