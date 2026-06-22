"""Security-headers middleware (SPEC §8 — internet-facing behind TLS proxy).

Sets HSTS + a conservative header set on every response. The app does this itself
rather than relying on the reverse proxy (defence in depth). CSP allows only
same-origin assets (the vendored Cytoscape script ships under /static).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)

_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": _CSP,
    "Cache-Control": "no-store",
}


async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    for header, value in _HEADERS.items():
        response.headers.setdefault(header, value)
    return response
