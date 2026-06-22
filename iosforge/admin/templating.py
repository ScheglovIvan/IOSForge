"""Shared Jinja2 templates instance + cookie helpers for the admin panel."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from iosforge.admin import session as sess
from iosforge.admin.csrf import PRE_COOKIE
from iosforge.common.config import get_settings

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        sess.COOKIE_NAME,
        sid,
        max_age=get_settings().admin_session_ttl_s,
        httponly=True,
        secure=get_settings().admin_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(sess.COOKIE_NAME, path="/")


def set_pre_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        PRE_COOKIE,
        token,
        max_age=600,
        httponly=True,
        secure=get_settings().admin_cookie_secure,
        samesite="lax",
        path="/",
    )
