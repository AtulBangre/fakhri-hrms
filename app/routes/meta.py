"""Meta endpoints: health, PWA manifests and the service worker."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import __version__
from ..config import BRAND
from ..deps import STATIC_DIR, get_face, get_settings_dep, get_storage
from ..services import orgtime

router = APIRouter(tags=["meta"])


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "service": "hrms", "version": __version__}


@router.get("/api/health")
def api_health(
    request: Request,
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
):
    """Used by the UI banner and the service worker to detect a degraded state."""
    db_ok = True
    try:
        storage.scalar("SELECT COUNT(*) AS n FROM employees")
    except Exception:  # pragma: no cover - defensive
        db_ok = False
    health = face.health()
    return JSONResponse(
        {
            "status": "ok" if db_ok else "degraded",
            "version": __version__,
            "time": orgtime.local_now(settings).isoformat(timespec="seconds"),
            "database": "ok" if db_ok else "error",
            "face_service": health,
            "face_required": settings.face_required_for_kiosk,
            "org": storage.get_setting("org_name", settings.org_name),
        }
    )


def _manifest(*, name: str, short_name: str, start_url: str, description: str, scope: str = "/") -> dict:
    return {
        "name": name,
        "short_name": short_name,
        "description": description,
        "start_url": start_url,
        "scope": scope,
        "display": "standalone",
        "orientation": "portrait",
        "background_color": BRAND["surface"],
        "theme_color": BRAND["primary"],
        "categories": ["business", "productivity"],
        "icons": [
            {"src": "/static/img/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/img/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/img/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Mark attendance", "url": "/kiosk"},
            {"name": "Today's board", "url": "/attendance"},
        ],
    }


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest(settings=Depends(get_settings_dep)) -> JSONResponse:
    return JSONResponse(
        _manifest(
            name=f"{settings.org_name} HRMS",
            short_name="HRMS",
            start_url="/dashboard",
            description="Attendance, people and payroll for Fakhri IT Services.",
        ),
        media_type="application/manifest+json",
    )


@router.get("/kiosk.webmanifest", include_in_schema=False)
def kiosk_manifest(settings=Depends(get_settings_dep)) -> JSONResponse:
    """Separate manifest so the gate phone installs straight into the scanner."""
    return JSONResponse(
        _manifest(
            name=f"{settings.org_name} Attendance Scanner",
            short_name="Scanner",
            start_url="/kiosk",
            description="Face attendance scanner for the gate.",
        ),
        media_type="application/manifest+json",
    )


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    """Served from the root so its scope covers the whole app."""
    return FileResponse(
        Path(STATIC_DIR) / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    icon = Path(STATIC_DIR) / "img" / "icon-192.png"
    if icon.exists():
        return FileResponse(icon, media_type="image/png")
    return Response(status_code=204)


@router.get("/employees/{employee_id}/avatar", include_in_schema=False)
def employee_avatar(
    employee_id: int,
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
) -> Response:
    """Stream an employee's registered face photo if available, else 404."""
    from ..uploads import path_for, UploadError
    row = storage.query_one("SELECT photo_filename FROM employees WHERE id = ?", (employee_id,))
    if not row or not row.get("photo_filename"):
        return Response(status_code=404)
    try:
        path = path_for(settings, "avatars", row["photo_filename"])
        if not path.exists():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg")
    except (UploadError, ValueError):
        return Response(status_code=404)


@router.get("/attendance/photos/{filename}", include_in_schema=False)
def punch_photo(
    filename: str,
    settings=Depends(get_settings_dep),
) -> Response:
    """Stream a captured punch snapshot (for HR audit), else 404."""
    from ..uploads import path_for, UploadError
    try:
        path = path_for(settings, "punches", filename)
        if not path.exists():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg")
    except (UploadError, ValueError):
        return Response(status_code=404)

