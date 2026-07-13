"""codemagic_integration stage/state + generation_results.codemagic

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-07 15:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'codemagic_integration'")
    op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'codemagic_integration'")
    op.add_column(
        "generation_results",
        sa.Column(
            "codemagic",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.alter_column("generation_results", "codemagic", server_default=None)


def downgrade() -> None:
    """Downgrade schema.

    Postgres enum values cannot be dropped, so ``codemagic_integration`` remains
    on the ``job_state`` / ``pipeline_stage`` types (harmless, unused).
    """
    op.drop_column("generation_results", "codemagic")
