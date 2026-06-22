"""Login / logout routes (SPEC §7 auth, §8 security).

Server-side sessions, argon2 password verify, double-submit CSRF on the login
form, and per-username/per-IP rate limiting with lockout. Failure messages are
deliberately generic; passwords/sessions are never logged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from iosforge.admin import csrf, ratelimit
from iosforge.admin import session as sess
from iosforge.admin.deps import current_session, get_db, require_user
from iosforge.admin.security import verify_password
from iosforge.admin.session import SessionData
from iosforge.admin.templating import (
    clear_session_cookie,
    set_pre_csrf_cookie,
    set_session_cookie,
    templates,
)
from iosforge.common.logging import get_logger
from iosforge.db.models import AdminUser

log = get_logger("admin.auth")
router = APIRouter()


def _client_ip(request: Request) -> str:
    # Behind the reverse proxy uvicorn populates request.client from
    # X-Forwarded-For when started with --proxy-headers.
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request) -> Response:
    if current_session(request) is not None:
        return RedirectResponse("/jobs", status_code=HTTP_303_SEE_OTHER)
    token = csrf.new_token()
    response = templates.TemplateResponse(
        request, "login.html", {"csrf_token": token, "error": None}
    )
    set_pre_csrf_cookie(response, token)
    return response


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    ip = _client_ip(request)
    cookie_token = request.cookies.get(csrf.PRE_COOKIE)
    if not csrf.verify(cookie_token, csrf_token):
        return _login_error(request, "Session expired, please try again.", status=400)

    if ratelimit.is_locked(username, ip):
        log.warning("auth.login_locked", ip=ip)
        return _login_error(request, "Too many attempts. Try again later.", status=429)

    user = db.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        ratelimit.record_failure(username, ip)
        log.warning("auth.login_failed", ip=ip)  # never log username/password
        return _login_error(request, "Invalid username or password.", status=401)

    ratelimit.clear(username, ip)
    sid, _ = sess.create_session(str(user.id), user.username)
    log.info("auth.login_ok", user_id=str(user.id))
    response = RedirectResponse("/jobs", status_code=HTTP_303_SEE_OTHER)
    set_session_cookie(response, sid)
    response.delete_cookie(csrf.PRE_COOKIE, path="/")
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return _login_error(request, "Invalid request.", status=400)
    sess.destroy_session(request.cookies.get(sess.COOKIE_NAME))
    response = RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


def _login_error(request: Request, message: str, *, status: int) -> Response:
    token = csrf.new_token()
    response = templates.TemplateResponse(
        request, "login.html", {"csrf_token": token, "error": message}, status_code=status
    )
    set_pre_csrf_cookie(response, token)
    return response
