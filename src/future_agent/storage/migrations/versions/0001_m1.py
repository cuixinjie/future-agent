"""Initial M1 schema. Downgrade explicitly refuses to delete user history."""

import sqlalchemy as sa
from alembic import op

revision = "0001_m1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(200), unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "event_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id", sa.String(36), sa.ForeignKey("analysis_sessions.id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("session_id", "revision"),
    )
    op.create_table(
        "evaluation_protocols",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("locked", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("初始迁移不可无损回退；请使用事先保存的数据库备份，禁止自动删除历史")
