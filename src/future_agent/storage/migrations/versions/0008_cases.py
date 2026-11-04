"""Separate historical case inputs, outcomes and dataset releases."""

import sqlalchemy as sa
from alembic import op

revision = "0008_cases"
down_revision = "0007_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_case_datasets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("domain", sa.String(20), nullable=False),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("import_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("domain", "version"),
    )
    op.create_index("ix_historical_case_datasets_domain", "historical_case_datasets", ["domain"])
    op.create_table(
        "historical_cases",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(36),
            sa.ForeignKey("historical_case_datasets.id"),
            nullable=False,
        ),
        sa.Column("case_key", sa.String(100), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("dataset_id", "case_key"),
    )
    op.create_index("ix_historical_cases_dataset_id", "historical_cases", ["dataset_id"])
    op.create_table(
        "historical_case_outcomes",
        sa.Column(
            "case_id", sa.String(200), sa.ForeignKey("historical_cases.id"), primary_key=True
        ),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("不可无损回退；请恢复升级前完整DATA_DIR备份")
