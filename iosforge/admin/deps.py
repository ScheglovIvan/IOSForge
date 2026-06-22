"""FastAPI dependencies for the admin panel: DB session, storage, auth guard."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from iosforge.admin import session as sess
from iosforge.admin.session import SessionData
from iosforge.db.session import get_sessionmaker
from iosforge.storage.client import ArtifactStorage, S3ArtifactStorage


class NotAuthenticated(Exception):
    """Raised by ``require_user`` when there is no valid session."""


def get_db() -> Iterator[Session]:
    maker = get_sessionmaker()
    db = maker()
    try:
        yield db
    finally:
        db.close()


def get_storage() -> ArtifactStorage:
    return S3ArtifactStorage()


def current_session(request: Request) -> SessionData | None:
    return sess.read_session(request.cookies.get(sess.COOKIE_NAME))


def require_user(request: Request) -> SessionData:
    data = current_session(request)
    if data is None:
        raise NotAuthenticated()
    request.state.session = data
    return data


CurrentUser = Depends(require_user)
Db = Depends(get_db)
Storage = Depends(get_storage)
