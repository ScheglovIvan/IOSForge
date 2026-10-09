"""xcode_builds table (native SwiftUI delivery: archive → IPA on a Mac worker)

Revision ID: a8b9c0d1e2f3
Revises: f7b0c1d2e3a4
Create Date: 2026-10-09 19:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "f7b0c1d2e3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "xcode_builds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("signed", sa.Boolean(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=True),
        sa.Column("build_number", sa.String(length=32), nullable=True),
        sa.Column("ipa_key", sa.String(length=1024), nullable=True),
        sa.Column("upload_command", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("log_text", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_xcode_builds_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_xcode_builds")),
    )
    op.create_index(op.f("ix_xcode_builds_job_id"), "xcode_builds", ["job_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_xcode_builds_job_id"), table_name="xcode_builds")
    op.drop_table("xcode_builds")
