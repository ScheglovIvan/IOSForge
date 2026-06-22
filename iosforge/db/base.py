"""SQLAlchemy 2.0 declarative base and shared column conventions (T-2.2, SPEC §10).

All ORM models inherit from :class:`Base`. A stable ``naming_convention`` is set on
the shared metadata so Alembic autogenerate produces deterministic constraint /
index names across runs (important for reproducible baseline migrations).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint naming -> stable Alembic diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all IOSForge ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def uuid_pk() -> Mapped[uuid.UUID]:
    """A UUID primary-key column generated client-side (SPEC §10: UUID PK)."""
    return mapped_column(primary_key=True, default=uuid.uuid4)


def utcnow() -> dt.datetime:
    """Timezone-aware UTC now (all timestamps are ``timestamptz``)."""
    return dt.datetime.now(tz=dt.UTC)
