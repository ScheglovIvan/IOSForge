"""Engine / sessionmaker wiring from application settings (T-2.2, SPEC §10).

The synchronous engine is built from ``Settings.database_url`` (psycopg / psql).
A lazily-created singleton engine is shared process-wide; tests and callers that
need isolation can build their own engine via :func:`create_engine_from_url`.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from iosforge.common.config import get_settings


def create_engine_from_url(database_url: str, *, echo: bool = False) -> Engine:
    """Build a synchronous SQLAlchemy engine for the given URL."""
    return create_engine(database_url, echo=echo, future=True, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the cached process-wide engine built from settings."""
    settings = get_settings()
    return create_engine_from_url(settings.database_url, echo=settings.debug)


def get_sessionmaker() -> sessionmaker[Session]:
    """Return a sessionmaker bound to the shared engine."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
