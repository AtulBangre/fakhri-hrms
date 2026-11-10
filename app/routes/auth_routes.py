"""Sign in / sign out for both the HR console and the gate kiosk."""

from __future__ import annotations

import base64
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

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
from ..config import Settings
from ..deps import get_face, get_session, get_settings_dep, get_storage, redirect, render
from ..face import FaceService, FaceServiceError, FaceServiceUnavailable
from ..services import employees as employees_service
from ..db import Storage
from ..uploads import read_capped

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


# ---------------------------------------------------- face recognition sign-in
@router.get("/api/auth/face-available")
def face_available(face: FaceService = Depends(get_face)):
    """Check if the face recognition service is healthy and available for login."""
    try:
        health = face.health()
        return {"available": bool(health.get("ok")), "detail": health.get("detail", "")}
    except Exception:
        return {"available": False, "detail": "unavailable"}


@router.post("/api/auth/face-login")
async def face_login(
    request: Request,
    response: Response,
    storage: Storage = Depends(get_storage),
    settings: Settings = Depends(get_settings_dep),
    face: FaceService = Depends(get_face),
):
    """Sign in an employee via facial recognition using their enrolled HRMS face."""
    ip = request.client.host if request.client else None
    failure_key = "face_login"

    # Rate limiting & lockout protection
    if auth.login_locked(storage, failure_key, ip):
        storage.audit(actor="unknown", action="login_locked", entity="user", ip=ip, detail="face_login_lockout")
        return JSONResponse(
            {"ok": False, "detail": "Too many failed sign-in attempts. Please wait 10 minutes or use your password."},
            status_code=429,
        )

    # Read image from JSON body or multipart form
    image_bytes = None
    next_url = ""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                next_url = str(body.get("next") or "")
                raw_image = body.get("image")
                if raw_image and isinstance(raw_image, str):
                    if "," in raw_image:
                        raw_image = raw_image.split(",", 1)[1]
                    image_bytes = base64.b64decode(raw_image)
        except Exception:
            return JSONResponse({"ok": False, "detail": "Invalid image payload."}, status_code=400)
    else:
        try:
            form = await request.form()
            next_url = str(form.get("next") or "")
            file_upload = form.get("image")
            if file_upload and hasattr(file_upload, "file"):
                image_bytes = read_capped(file_upload.file)
        except Exception:
            return JSONResponse({"ok": False, "detail": "Could not read image upload."}, status_code=400)

    if not image_bytes:
        return JSONResponse(
            {"ok": False, "detail": "No image provided. Please allow camera access and look at the screen."},
            status_code=400,
        )

    # Face API inference
    try:
        identification = face.identify(image_bytes, checks=True)
    except FaceServiceUnavailable:
        return JSONResponse(
            {"ok": False, "detail": "Face service is temporarily unavailable. Please sign in with your password."},
            status_code=503,
        )
    except FaceServiceError as exc:
        return JSONResponse(
            {"ok": False, "detail": exc.message or "Could not process face scan. Please try again."},
            status_code=400,
        )

    # Probe validation: face detected?
    if (identification.faces_in_probe or 0) == 0:
        return JSONResponse(
            {"ok": False, "detail": "No face detected in the frame. Please look directly into the camera in good lighting."},
            status_code=400,
        )

    # Anti-spoofing / liveness check
    if identification.decision == "spoof" or identification.is_live is False:
        auth.record_login_failure(storage, failure_key, ip)
        storage.audit(
            actor="unknown",
            action="login_spoof_rejected",
            entity="user",
            detail=f"reason={identification.spoof_reason or 'spoof'} score={identification.liveness_score}",
            ip=ip,
        )
        return JSONResponse(
            {"ok": False, "detail": "Liveness check failed. Please face the camera directly in person."},
            status_code=400,
        )

    # Retry decision (e.g. blurry, poor lighting, etc.)
    if identification.decision == "retry":
        reasons = {
            "too_far": "Please move a bit closer to the camera.",
            "too_dark": "Too dark. Please move to a brighter area.",
            "too_bright": "Too bright. Avoid direct light behind you.",
            "too_blurry": "Image was blurry. Please hold steady.",
            "face_unclear": "Face was unclear. Look straight at the camera.",
            "multiple_faces": "Only one person should be in the camera frame.",
        }
        msg = reasons.get(identification.retry_reason, "Please look straight at the camera and hold still.")
        return JSONResponse({"ok": False, "detail": msg}, status_code=400)

    # Match employee via resolve_face_match
    resolution = employees_service.resolve_face_match(storage, identification)
    if resolution.employee is None:
        auth.record_login_failure(storage, failure_key, ip)
        storage.audit(actor="unknown", action="login_face_unrecognised", entity="user", ip=ip)
        return JSONResponse(
            {"ok": False, "detail": "Face not recognised. Please ensure your face is registered in HRMS, or sign in with password."},
            status_code=401,
        )

    employee = resolution.employee
    # Verify employee status
    if employee.get("status") != "active":
        return JSONResponse(
            {"ok": False, "detail": "This employee account is inactive. Please contact HR."},
            status_code=403,
        )

    # Lookup or create employee's portal user account
    user = auth.get_user_for_employee(storage, employee["id"])
    if user is None:
        code = str(employee.get("code") or f"emp{employee['id']}").strip().lower()
        if auth.get_user_by_username(storage, code):
            code = f"emp{employee['id']}"
        user = auth.create_user(
            storage,
            username=code,
            password=auth.generate_password(),
            role=auth.ROLE_EMPLOYEE,
            display_name=employee.get("name") or code,
            employee_id=employee["id"],
        )

    if not user.get("active", 1):
        return JSONResponse(
            {"ok": False, "detail": "Your portal login is disabled. Please contact HR."},
            status_code=403,
        )

    # Successful login: clear failures & issue session cookie
    auth.clear_login_failures(storage, failure_key)
    auth.clear_login_failures(storage, user["username"])
    lifetime = settings.hr_session_hours * 3600
    token, session = create_session_token(user, settings, lifetime_seconds=lifetime)

    storage.audit(
        actor=user["username"],
        action="login",
        entity="user",
        entity_id=user["id"],
        detail=f"{user['role']} via face recognition (similarity={resolution.similarity:.2f})",
        ip=ip,
    )

    target = next_url if is_safe_redirect(next_url) else _home_for(session)
    res = JSONResponse({
        "ok": True,
        "redirect": target,
        "name": employee.get("name") or user.get("display_name"),
    })
    set_session_cookie(res, token, settings, max_age=lifetime)
    return res

