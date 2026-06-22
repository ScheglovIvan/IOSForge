"""Login rate limiting + lockout in Redis (SPEC §8 — brute-force protection).

Counts failed login attempts per username and per client IP within a rolling
window; once either exceeds the configured max, further attempts are locked out
until the window expires. Successful login clears the username counter.
"""

from __future__ import annotations

import redis

from iosforge.common.config import get_settings

_PREFIX = "iosforge:loginfail:"


def _client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def _key(scope: str, ident: str) -> str:
    return f"{_PREFIX}{scope}:{ident}"


def is_locked(username: str, ip: str) -> bool:
    """True if either the username or the IP has exceeded the attempt limit."""
    s = get_settings()
    client = _client()
    for scope, ident in (("user", username.lower()), ("ip", ip)):
        val = client.get(_key(scope, ident))
        if val is not None and int(val) >= s.admin_login_max_attempts:  # type: ignore[arg-type]
            return True
    return False


def record_failure(username: str, ip: str) -> None:
    s = get_settings()
    client = _client()
    for scope, ident in (("user", username.lower()), ("ip", ip)):
        key = _key(scope, ident)
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, s.admin_login_lockout_s)
        pipe.execute()


def clear(username: str, ip: str) -> None:
    client = _client()
    client.delete(_key("user", username.lower()), _key("ip", ip))
