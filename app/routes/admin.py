"""Settings, user accounts, holidays and the face-service health panel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from .. import auth
from ..auth import (
    SessionUser,
    change_password,
    create_user,
    delete_user,
    list_users,
    require_hr,
    set_user_active,
    verify_form_csrf,
)
from ..config import SETTING_DEFS
from ..deps import get_face, get_settings_dep, get_storage, redirect, render
from ..face import FaceServiceUnavailable
from ..services import employees as employees_service
from ..services import orgtime

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
):
    health = face.health()
    collection = None
    if health.get("ok"):
        try:
            collection = face.collection_stats()
        except FaceServiceUnavailable as exc:
            collection = {"error": str(exc)}

    return render(
        request,
        "settings.html",
        active="settings",
        user=user,
        defs=SETTING_DEFS,
        values=storage.all_settings(),
        weekday_names=orgtime.WEEKDAY_NAMES,
        week_off_label=orgtime.week_off_label(storage),
        users=list_users(storage),
        holidays=storage.query("SELECT * FROM holidays ORDER BY day"),
        health=health,
        collection=collection,
        employee_stats=employees_service.stats(storage),
        credentials_file=str(settings.data_dir / auth.CREDENTIALS_FILE),
        faceapi_url=settings.faceapi_url,
        collection_name=settings.face_collection,
    )


@router.post("/value")
def save_value(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    key: str = Form(...),
    value: str = Form(""),
    value_multi: list[str] = Form([]),
):
    verify_form_csrf(user, csrf)
    kinds = {definition[0]: definition[3] for definition in SETTING_DEFS}
    if key not in kinds:
        return redirect("/settings", err=f"Unknown setting '{key}'.")
    if kinds[key] == "weekdays":
        # checkboxes: an unticked box is simply absent, so "none ticked" is valid
        value = ",".join(str(day) for day in sorted(orgtime.parse_weekdays(",".join(value_multi))))
    elif key == "saturday_off_weeks":
        weeks = sorted(w for w in orgtime.parse_weekdays(value) if 1 <= w <= 5)
        value = ",".join(str(week) for week in weeks)
    storage.set_setting(key, value.strip())
    storage.audit(actor=user.username, action="update_setting", entity="settings", entity_id=key,
                  detail=value.strip()[:120])
    return redirect("/settings", ok=f"Setting saved: {key}.")


@router.post("/values")
async def save_values(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
):
    """Save the whole rules form at once (only keys that actually changed)."""
    form = await request.form()
    verify_form_csrf(user, str(form.get("_csrf") or ""))
    current = storage.all_settings()
    changed = []
    for key, default, _label, kind, _help in SETTING_DEFS:
        if kind == "weekdays":
            if not form.get(f"{key}__present"):
                continue
            value = ",".join(str(d) for d in sorted(orgtime.parse_weekdays(",".join(form.getlist(key)))))
        elif key in form:
            value = str(form.get(key) or "").strip()
            if key == "saturday_off_weeks":
                value = ",".join(str(w) for w in sorted(w for w in orgtime.parse_weekdays(value) if 1 <= w <= 5))
            if kind == "number" and value:
                try:
                    float(value)
                except ValueError:
                    return redirect("/settings", err=f"{_label} must be a number.")
        else:
            continue
        if value != current.get(key, default):
            storage.set_setting(key, value)
            changed.append(key)
    if changed:
        storage.audit(actor=user.username, action="update_settings", entity="settings",
                      detail=", ".join(changed)[:200])
        return redirect("/settings", ok=f"Saved {len(changed)} setting(s).")
    return redirect("/settings", ok="No changes to save.")


@router.post("/users")
def add_user(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("hr"),
    display_name: str = Form(""),
    device_label: str = Form(""),
    email: str = Form(""),
):
    verify_form_csrf(user, csrf)
    if role not in (auth.ROLE_HR, auth.ROLE_KIOSK):
        return redirect("/settings", err="Role must be hr or kiosk.")
    if len(password) < 8:
        return redirect("/settings", err="Password must be at least 8 characters.")
    if auth.get_user_by_username(storage, username):
        return redirect("/settings", err=f"Username '{username}' already exists.")
    created = create_user(
        storage, username=username, password=password, role=role,
        display_name=display_name or username, device_label=device_label or None,
    )
    if email.strip():
        storage.execute("UPDATE users SET email = ? WHERE id = ?", (email.strip().lower(), created["id"]))
    storage.audit(actor=user.username, action="create_user", entity="user", entity_id=username, detail=role)
    return redirect("/settings", ok=f"{role.upper()} login created for '{username}'.")


@router.post("/users/{user_id}/password")
def reset_password(
    request: Request,
    user_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    password: str = Form(...),
):
    verify_form_csrf(user, csrf)
    if len(password) < 8:
        return redirect("/settings", err="Password must be at least 8 characters.")
    change_password(storage, user_id, password)
    storage.audit(actor=user.username, action="reset_password", entity="user", entity_id=user_id)
    return redirect("/settings", ok="Password updated.")


@router.post("/users/{user_id}/status")
def toggle_user(
    request: Request,
    user_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    active: str = Form("1"),
):
    verify_form_csrf(user, csrf)
    if user_id == user.id and active != "1":
        return redirect("/settings", err="You cannot disable your own account.")
    set_user_active(storage, user_id, active == "1")
    storage.audit(actor=user.username, action="set_user_active", entity="user", entity_id=user_id, detail=active)
    return redirect("/settings", ok="User updated.")


@router.post("/users/{user_id}/email")
def set_email(
    request: Request,
    user_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    email: str = Form(""),
):
    verify_form_csrf(user, csrf)
    value = email.strip().lower()
    if value and ("@" not in value or "." not in value.split("@")[-1]):
        return redirect("/settings", err="Enter a valid email address.")
    storage.execute("UPDATE users SET email = ? WHERE id = ?", (value or None, user_id))
    storage.audit(actor=user.username, action="set_user_email", entity="user", entity_id=user_id, detail=value)
    return redirect("/settings", ok="Email saved." if value else "Email removed.")


@router.post("/users/{user_id}/signout")
def sign_out_everywhere(
    request: Request,
    user_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    auth.revoke_sessions(storage, user_id)
    storage.audit(actor=user.username, action="revoke_sessions", entity="user", entity_id=user_id)
    if user_id == user.id:
        return redirect("/login", ok="Signed out on every device.")
    return redirect("/settings", ok="That login was signed out on every device.")


@router.post("/users/{user_id}/delete")
def remove_user(
    request: Request,
    user_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    if user_id == user.id:
        return redirect("/settings", err="You cannot delete your own account.")
    delete_user(storage, user_id)
    storage.audit(actor=user.username, action="delete_user", entity="user", entity_id=user_id)
    return redirect("/settings", ok="Login deleted.")
