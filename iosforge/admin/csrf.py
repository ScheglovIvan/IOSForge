"""CSRF protection helpers (SPEC §8).

Two patterns, both backed by a high-entropy token and constant-time comparison:
* authenticated forms use the per-session token (synchronizer pattern);
* the login form (no session yet) uses a double-submit ``csrf_pre`` cookie.
SameSite=Lax on the session/pre cookies blocks cross-site POSTs as a second layer.
"""

from __future__ import annotations

import hmac
import secrets

PRE_COOKIE = "csrf_pre"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def verify(expected: str | None, submitted: str | None) -> bool:
    """Constant-time equality; False if either side is missing/empty."""
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)
