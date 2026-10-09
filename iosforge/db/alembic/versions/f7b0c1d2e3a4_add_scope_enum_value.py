"""add scope enum value to job_state and pipeline_stage

Supports the new Stage SCOPE feature (feasibility + scope gate between ANALYSIS
and CODEGEN, see DECISIONS.md 2026-10-09 SwiftUI pivot). Adds the ``scope``
label to both native Postgres enums backing the Job model. ``ALTER TYPE ... ADD
VALUE`` cannot run inside a transaction, so it executes in an autocommit block.

The native SQLAlchemy ``Enum(PyEnum, native_enum=True)`` columns persist member
NAMES (uppercase), so the ORM emits ``SCOPE`` for ``Stage.SCOPE`` /
``JobState.SCOPE``; the uppercase label is what makes writes succeed (same
lesson as revision e3a9d709d350). The lowercase ``scope`` label is added too for
parity with the existing ``verify`` / ``github_upload`` labels.

Revision ID: f7b0c1d2e3a4
Revises: e3a9d709d350
Create Date: 2026-10-09 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7b0c1d2e3a4"
down_revision: str | Sequence[str] | None = "e3a9d709d350"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'scope'")
        op.execute("ALTER TYPE pipeline_stage ADD VALUE IF NOT EXISTS 'SCOPE'")
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'scope'")
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'SCOPE'")


def downgrade() -> None:
    """Downgrade schema (Postgres enum values cannot be dropped; irreversible)."""
