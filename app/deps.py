"""Shared FastAPI dependencies, Jinja setup and small view helpers."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from .auth import SessionUser, current_user, get_session
from .config import BRAND, Settings
from .db import Storage
from .face import FaceService
from .services import orgtime

logger = logging.getLogger("hrms.web")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_face(request: Request) -> FaceService:
    return request.app.state.face


# ------------------------------------------------------------------- filters
def money(value: Any, symbol: str = "₹") -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return f"{symbol}0"
    sign = "-" if amount < 0 else ""
    whole = f"{abs(amount):,.0f}"
    paise = f"{abs(amount):,.2f}".split(".")[1]
    return f"{sign}{symbol}{whole}" if paise == "00" else f"{sign}{symbol}{abs(amount):,.2f}"


def number(value: Any, decimals: int = 0) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    if decimals:
        return f"{amount:,.{decimals}f}"
    return f"{amount:,.0f}".rstrip("0").rstrip(".") if amount % 1 else f"{amount:,.0f}"


def nice_date(value: Any, fmt: str = "%d %b %Y") -> str:
    if not value:
        return "—"
    if isinstance(value, (date, datetime)):
        return value.strftime(fmt)
    text = str(value)
    for candidate in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(candidate) + 2].strip(), candidate).strftime(fmt)
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10]).strftime(fmt)
    except ValueError:
        return text


def initials(value: Any) -> str:
    """Two-letter monogram that tolerates a SessionUser, a row dict or a string."""
    if value is None:
        return "?"
    name = getattr(value, "display_name", None) or getattr(value, "name", None)
    if name is None and isinstance(value, dict):
        name = value.get("display_name") or value.get("name")
    if name is None:
        name = getattr(value, "username", None) or str(value)
    parts = [part for part in str(name).split() if part]
    return "".join(part[0].upper() for part in parts[:2]) or "?"


def status_chip(status: str | None) -> str:
    """CSS class for an attendance status."""
    return {
        orgtime.STATUS_PRESENT: "chip chip-ok",
        orgtime.STATUS_HALF_DAY: "chip chip-warn",
        orgtime.STATUS_LEAVE: "chip chip-info",
        orgtime.STATUS_ABSENT: "chip chip-danger",
        orgtime.STATUS_WEEK_OFF: "chip chip-muted",
        orgtime.STATUS_HOLIDAY: "chip chip-muted",
        orgtime.STATUS_PENDING: "chip chip-ghost",
    }.get(status or "", "chip")


templates.env.filters.update(
    {
        "money": money,
        "number": number,
        "nice_date": nice_date,
        "initials": initials,
        "chip": status_chip,
        "clock": orgtime.format_clock,
        "duration": orgtime.format_minutes,
    }
)
def compute_asset_version(static_dir: Path | None = None) -> str:
    """Short hash of every CSS/JS asset.

    Templates link ``app.css?v=<hash>``, so changing a stylesheet changes the URL:
    caches (browser *and* service worker) can never serve a stale file, and the
    assets can be cached forever with ``immutable``.
    """
    base = Path(static_dir) if static_dir else STATIC_DIR
    digest = hashlib.sha256()
    candidates = sorted((base / "css").glob("*.css")) + sorted((base / "js").glob("*.js"))
    candidates.append(base / "sw.js")
    for path in candidates:
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = compute_asset_version()

templates.env.filters["zip"] = lambda left, right: zip(left, right)
templates.env.filters["min"] = min
templates.env.filters["max"] = max
templates.env.globals.update(
    brand=BRAND,
    app_version=__version__,
    asset_version=ASSET_VERSION,
    STATUS_LABELS=orgtime.STATUS_LABELS,
    LEAVE_LABELS=orgtime.LEAVE_LABELS,
    today_iso=lambda: date.today().isoformat(),
)


# ------------------------------------------------------------------ rendering
def flash_url(path: str, *, ok: str | None = None, err: str | None = None, **params: Any) -> str:
    query = {k: v for k, v in params.items() if v not in (None, "")}
    if ok:
        query["ok"] = ok
    if err:
        query["err"] = err
    return f"{path}?{urlencode(query)}" if query else path


def redirect(path: str, **kwargs: Any) -> RedirectResponse:
    return RedirectResponse(flash_url(path, **kwargs), status_code=303)


def page_context(request: Request, *, active: str, user: SessionUser | None, **extra: Any) -> dict[str, Any]:
    settings = get_settings_dep(request)
    storage = get_storage(request)
    org_name = storage.get_setting("org_name", settings.org_name)
    context: dict[str, Any] = {
        "request": request,
        "active": active,
        "user": user,
        "org_name": org_name,
        "settings": settings,
        "ok_message": request.query_params.get("ok"),
        "err_message": request.query_params.get("err"),
        "today": orgtime.today(settings),
        "csrf_token": user.csrf if user else "",
        "nav_items": [],
    }
    context.update(extra)
    return context


def render(
    request: Request,
    template: str,
    *,
    active: str,
    user: SessionUser | None = None,
    status_code: int = 200,
    **extra: Any,
):
    return templates.TemplateResponse(
        request,
        template,
        page_context(request, active=active, user=user, **extra),
        status_code=status_code,
    )


def html_or_json_error(request: Request, message: str, status_code: int = 400):
    """Small helper for API routes that want a consistent JSON error."""
    return HTMLResponse(
        f'{{"error": {{"message": {message!r}}}}}', status_code=status_code, media_type="application/json"
    )


__all__ = [
    "BRAND",
    "HTTPException",
    "SessionUser",
    "ASSET_VERSION",
    "STATIC_DIR",
    "current_user",
    "get_face",
    "get_session",
    "get_settings_dep",
    "get_storage",
    "page_context",
    "redirect",
    "render",
    "templates",
]
