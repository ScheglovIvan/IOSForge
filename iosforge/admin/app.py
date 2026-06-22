"""FastAPI application factory for the IOSForge admin panel (SPEC §7).

Wires server-side-session auth, APK upload, job tracking and artifact views on top
of the EPIC-2 infra (DB/Celery/MinIO). Internet-facing: it sets security headers
itself and is meant to run on 127.0.0.1 behind a TLS reverse proxy (see README).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from iosforge.admin import auth, routes_jobs
from iosforge.admin.deps import NotAuthenticated
from iosforge.admin.middleware import security_headers


def create_app() -> FastAPI:
    """Build and return the IOSForge admin FastAPI application."""
    app = FastAPI(title="IOSForge Admin", version="0.1.0", docs_url=None, redoc_url=None)

    app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)

    @app.exception_handler(NotAuthenticated)
    async def _auth_redirect(request: Request, exc: NotAuthenticated) -> Response:
        # Browser navigations -> redirect to login; everything else -> 401.
        accepts_html = "text/html" in request.headers.get("accept", "")
        if request.method == "GET" and accepts_html:
            return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": "authentication required"}, status_code=401)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(routes_jobs.router)
    return app


app = create_app()
