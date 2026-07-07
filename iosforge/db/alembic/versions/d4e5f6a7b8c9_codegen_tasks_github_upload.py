"""codegen_tasks table + github_upload stage/state + generation github_repo_url

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-07 15:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'github_upload'")
    op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'github_upload'")

    op.add_column(
        "generation_results", sa.Column("github_repo_url", sa.String(length=512), nullable=True)
    )

    op.create_table(
        "codegen_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("task_key", sa.String(length=256), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_codegen_tasks_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_codegen_tasks")),
    )
    op.create_index(op.f("ix_codegen_tasks_job_id"), "codegen_tasks", ["job_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Postgres enum values cannot be dropped, so ``github_upload`` remains on the
    ``job_state`` / ``pipeline_stage`` types (harmless, unused).
    """
    op.drop_index(op.f("ix_codegen_tasks_job_id"), table_name="codegen_tasks")
    op.drop_table("codegen_tasks")
    op.drop_column("generation_results", "github_repo_url")
