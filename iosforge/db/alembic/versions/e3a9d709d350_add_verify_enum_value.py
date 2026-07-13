"""add verify enum value to job_state and pipeline_stage

Supports the new Stage VERIFY feature (structural web navigation audit +
hard-gate loop, see DECISIONS.md 2026-07-10). Adds the ``verify`` label to both
native Postgres enums backing the Job model. ``ALTER TYPE ... ADD VALUE`` cannot
run inside a transaction, so it executes in an autocommit block.

The native SQLAlchemy ``Enum(PyEnum, native_enum=True)`` columns persist member
NAMES (uppercase), so the ORM emits ``VERIFY`` for ``Stage.VERIFY`` /
``JobState.VERIFY``; the uppercase label is what makes writes succeed (same
lesson as revision a7b8c9d0e1f2). The lowercase ``verify`` label is added too for
parity with the existing ``github_upload`` / ``codemagic_integration`` labels.

Revision ID: e3a9d709d350
Revises: a7b8c9d0e1f2
Create Date: 2026-07-10 15:00:41.795735

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a9d709d350"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'verify'")
        op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'VERIFY'")
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'verify'")
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'VERIFY'")


def downgrade() -> None:
    """Downgrade schema (Postgres enum values cannot be dropped; irreversible)."""
