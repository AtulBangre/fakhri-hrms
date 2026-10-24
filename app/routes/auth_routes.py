"""Sign in / sign out for both the HR console and the gate kiosk."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse

from urllib.parse import urlsplit

from .. import auth
from ..auth import (
    ROLE_EMPLOYEE,
    ROLE_KIOSK,
    authenticate,
    clear_session_cookie,
    create_session_token,
    set_session_cookie,
    verify_form_csrf,
)
from ..deps import get_session, get_settings_dep, get_storage, redirect, render

logger = logging.getLogger("hrms.auth")
router = APIRouter(tags=["auth"])


def is_safe_redirect(url: str | None) -> bool:
    """Ensure the target is a safe local path (rejecting protocol-relative and external URLs)."""
    if not url or not url.startswith("/"):
        return False
    if url.startswith(("//", "/\\")):
        return False
    parsed = urlsplit(url)
    return not parsed.netloc and not parsed.scheme


@router.get("/login")
def login_page(request: Request, next: str | None = None):
    settings = get_settings_dep(request)
    storage = get_storage(request)
    session = get_session(request)
    if session is not None:
        return RedirectResponse(_home_for(session), status_code=303)
    return render(
        request,
        "login.html",
        active="login",
        user=None,
        next_url=next or "",
        demo_hint=(settings.data_dir / "initial_credentials.txt").exists(),
    )


@router.post("/login")
def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    settings = get_settings_dep(request)
    storage = get_storage(request)
    ip = request.client.host if request.client else None
    if auth.login_locked(storage, username, ip):
        storage.audit(actor=username[:40], action="login_locked", entity="user", ip=ip)
        return render(
            request, "login.html", active="login", user=None,
            error="Too many failed sign-ins. Wait 10 minutes and try again, or reset your password.",
            username_value=username, next_url=next,
            demo_hint=(settings.data_dir / "initial_credentials.txt").exists(),
            status_code=429,
        )
    user = authenticate(storage, username, password)

    if user is None:
        auth.record_login_failure(storage, username, ip)
        storage.audit(
            actor=username[:40], action="login_failed", entity="user",
            ip=ip,
        )
        return render(
            request,
            "login.html",
            active="login",
            user=None,
            error="Incorrect username or password.",
            username_value=username,
            next_url=next,
            demo_hint=(settings.data_dir / "initial_credentials.txt").exists(),
            status_code=401,
        )

    auth.clear_login_failures(storage, username)
    lifetime = settings.kiosk_session_days * 86400 if user["role"] == ROLE_KIOSK else settings.hr_session_hours * 3600
    token, session = create_session_token(user, settings, lifetime_seconds=lifetime)
    storage.audit(
        actor=user["username"], action="login", entity="user", entity_id=user["id"],
        detail=user["role"], ip=request.client.host if request.client else None,
    )

    if is_safe_redirect(next):
        target = next
    elif user["role"] == ROLE_KIOSK:
        target = "/kiosk"
    elif user["role"] == ROLE_EMPLOYEE:
        target = "/me"
    else:
        target = "/dashboard"

    redirect_response = RedirectResponse(target, status_code=303)
    set_session_cookie(redirect_response, token, settings, max_age=lifetime)
    return redirect_response


@router.post("/logout")
def logout(request: Request, csrf: str = Form("", alias="_csrf")):
    session = get_session(request)
    storage = get_storage(request)
    settings = get_settings_dep(request)
    if session:
        verify_form_csrf(session, csrf)
        storage.audit(actor=session.username, action="logout", entity="user", entity_id=session.id)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


def _home_for(session) -> str:
    if session.is_kiosk:
        return "/kiosk"
    if session.is_employee:
        return "/me"
    return "/dashboard"


@router.get("/")
def root(request: Request):
    session = get_session(request)
    if session is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(_home_for(session), status_code=303)


@router.get("/offline")
def offline(request: Request):
    return render(request, "offline.html", active="", user=get_session(request))
