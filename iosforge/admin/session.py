"""Server-side sessions backed by Redis (SPEC §7 auth, §8 security).

A session is an opaque high-entropy id stored as the ``sid`` cookie; all session
state lives server-side in Redis under ``iosforge:session:<sid>`` with a TTL, so
logout / expiry is real invalidation (not just a discarded signed cookie). The
cookie is HttpOnly + Secure + SameSite=Lax.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

import redis

from iosforge.common.config import get_settings

COOKIE_NAME = "sid"
_PREFIX = "iosforge:session:"


@dataclass
class SessionData:
    user_id: str
    username: str
    csrf_token: str


def _client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def create_session(user_id: str, username: str) -> tuple[str, SessionData]:
    """Create a new server-side session; return (sid, data)."""
    sid = secrets.token_urlsafe(32)
    data = SessionData(user_id=user_id, username=username, csrf_token=secrets.token_urlsafe(32))
    ttl = get_settings().admin_session_ttl_s
    _client().setex(_PREFIX + sid, ttl, json.dumps(data.__dict__))
    return sid, data


def read_session(sid: str | None) -> SessionData | None:
    """Return the session for ``sid`` (and slide its TTL), or None."""
    if not sid:
        return None
    client = _client()
    raw = client.get(_PREFIX + sid)
    if raw is None:
        return None
    client.expire(_PREFIX + sid, get_settings().admin_session_ttl_s)  # sliding expiry
    return SessionData(**json.loads(raw))  # type: ignore[arg-type]


def destroy_session(sid: str | None) -> None:
    if sid:
        _client().delete(_PREFIX + sid)
