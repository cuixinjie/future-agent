"""Add immutable historical datasets; never rewrite existing forecasts."""

import sqlalchemy as sa
from alembic import op

revision = "0007_reference"
down_revision = "0006_evaluation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_diagnostics",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "historical_datasets",
        sa.Column("version", sa.String(200), nullable=False),
        sa.UniqueConstraint("repository", "product", "channel", "version"),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository", sa.String(200), nullable=False),
        sa.Column("product", sa.String(200), nullable=False),
        sa.Column("channel", sa.String(200), nullable=False),
        sa.Column("import_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_historical_datasets_repository", "historical_datasets", ["repository"])


def downgrade() -> None:
    raise RuntimeError("不可无损回退，拒绝删除历史数据；回退请恢复升级前完整DATA_DIR备份")
