"""FastAPI application factory for the Fakhri HRMS.

Run locally::

    ./.venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8100

or use the helper, which also prints the generated logins::

    ./.venv/bin/python3 cli.py serve
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import ensure_default_users
from .config import BRAND, Settings, get_settings
from .db import Storage
from .deps import STATIC_DIR, get_session, templates
from .face import FaceService
from .notify import NotificationWorker
from .services.sync_supabase import SupabaseSyncWorker
from .routes import (
    account, admin, ai_hr, attendance, auth_routes, dashboard, employees,
    appraisals, engagement, exits, field, kiosk, leave, me, meta, payroll, records, regularizations, tax,
)

logger = logging.getLogger("hrms")

#: Inline handlers (print, auto-submit) need 'unsafe-inline' for scripts; framing,
#: plugins and third-party connections are shut off entirely.
SECURITY_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(self)",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
    ),
}


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    app.state.storage = Storage(settings)
    app.state.face = FaceService(settings)
    app.state.notifier = NotificationWorker(app.state.storage, settings)
    app.state.notifier.start()
    app.state.supabase_sync = SupabaseSyncWorker(app.state.storage, settings)
    app.state.supabase_sync.start()

    created = ensure_default_users(app.state.storage, settings)
    health = app.state.face.health()

    logger.info("=" * 70)
    logger.info("%s v%s starting", settings.app_name, __version__)
    logger.info("database      : %s", settings.db_path)
    logger.info("timezone      : %s", settings.timezone)
    logger.info("face service  : %s (%s)", settings.faceapi_url, "connected" if health.get("ok") else health.get("detail"))
    logger.info("face collection: %s", settings.face_collection)
    if created:
        logger.info("-" * 70)
        logger.info("FIRST RUN — logins created (also saved to data/%s):", "initial_credentials.txt")
        for role, username, password in created:
            logger.info("  %-6s %-12s %s", role, username, password)
        logger.info("-" * 70)
    logger.info("HR console    : %s/dashboard", settings.public_base_url.rstrip("/"))
    logger.info("Gate kiosk    : %s/kiosk", settings.public_base_url.rstrip("/"))
    logger.info("=" * 70)

    if not health.get("ok"):
        logger.warning(
            "Face service not reachable — attendance can still be marked manually by HR, "
            "but the kiosk scanner will report itself offline."
        )

    try:
        yield
    finally:
        if hasattr(app.state.face, "close"):
            app.state.face.close()
        if hasattr(app.state, "supabase_sync"):
            app.state.supabase_sync.stop()
        app.state.notifier.stop()
        app.state.storage.close()
        logger.info("HRMS stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    app = FastAPI(
        title=f"{settings.org_name} HRMS",
        version=__version__,
        description=(
            "Attendance (face recognition), employees, leave and payroll.\n\n"
            "Two roles: **hr** (everything) and **kiosk** (attendance marking only)."
        ),
        docs_url="/api/docs" if settings.api_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.api_docs else None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # Same policy the Caddy proxy sets, so a deployment without it is covered too (SEC-07).
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers["X-Processing-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"

        # Cache policy. Assets are linked with ?v=<content hash>, so an unchanged
        # URL really is immutable and a changed file gets a new URL — that is what
        # stops phones from running week-old CSS.
        path = request.url.path
        if path.startswith("/static/"):
            if "v" in request.query_params:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        elif path in ("/sw.js", "/manifest.webmanifest", "/kiosk.webmanifest"):
            response.headers["Cache-Control"] = "no-cache"
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(403)
    async def forbidden(request: Request, exc):  # noqa: ANN001
        detail = getattr(exc, "detail", None) or "You do not have access to this page."
        if request.url.path.startswith(("/api/", "/static/")) or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": {"message": detail}, "detail": detail}, status_code=403)
        session = get_session(request)
        return templates.TemplateResponse(
            request, "error.html",
            {"request": request, "active": "", "user": session, "org_name": settings.org_name, "brand": BRAND,
             "code": 403, "title": "Not allowed", "detail": detail, "ok_message": None, "err_message": None,
             "csrf_token": session.csrf if session else "", "today": None, "settings": settings},
            status_code=403,
        )

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        if request.url.path.startswith(("/api/", "/static/")):
            return JSONResponse({"error": {"message": "Not found"}}, status_code=404)
        session = get_session(request)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "request": request,
                "active": "",
                "user": session,
                "org_name": settings.org_name,
                "brand": BRAND,
                "code": 404,
                "title": "Page not found",
                "detail": "That page does not exist. Check the address or head back to the dashboard.",
                "ok_message": None,
                "err_message": None,
                "csrf_token": session.csrf if session else "",
                "today": None,
                "settings": settings,
            },
            status_code=404,
        )

    app.include_router(auth_routes.router)
    app.include_router(meta.router)
    app.include_router(dashboard.router)
    app.include_router(employees.router)
    app.include_router(regularizations.router)
    app.include_router(attendance.router)
    app.include_router(leave.router)
    app.include_router(payroll.router)
    app.include_router(me.router)
    app.include_router(account.router)
    app.include_router(engagement.router)
    app.include_router(records.router)
    app.include_router(tax.router)
    app.include_router(exits.router)
    app.include_router(appraisals.router)
    app.include_router(field.router)
    app.include_router(admin.router)
    app.include_router(kiosk.router)
    app.include_router(ai_hr.router)

    return app


app = create_app()
