"""fix enum case: add UPPERCASE github_upload/codemagic_integration labels

The native SQLAlchemy Enum persists member NAMES (uppercase), but the two prior
migrations added the lowercase member VALUES to the ``job_state`` /
``pipeline_stage`` types, so writing ``GITHUB_UPLOAD`` / ``CODEMAGIC_INTEGRATION``
failed. Add the uppercase labels the ORM actually emits.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-09 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'GITHUB_UPLOAD'")
    op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'CODEMAGIC_INTEGRATION'")
    op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'GITHUB_UPLOAD'")
    op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'CODEMAGIC_INTEGRATION'")


def downgrade() -> None:
    """Downgrade schema (Postgres enum values cannot be dropped)."""
