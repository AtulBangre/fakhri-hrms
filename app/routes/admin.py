"""Settings, user accounts, holidays and the face-service health panel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse

from .. import auth, notify
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
from ..db import utcnow_iso
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
        shifts=storage.query("SELECT * FROM shifts ORDER BY is_default DESC, name ASC"),
        users=list_users(storage),
        holidays=storage.query("SELECT * FROM holidays ORDER BY day"),
        health=health,
        collection=collection,
        employee_stats=employees_service.stats(storage),
        credentials_file=str(settings.data_dir / auth.CREDENTIALS_FILE),
        faceapi_url=settings.faceapi_url,
        collection_name=settings.face_collection,
        whatsapp_info=notify.get_waha_session_info(settings),
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
        # If shift timing or grace/half-day rules changed, keep the default shift in sync
        if any(k in changed for k in ("shift_start", "shift_end", "late_grace_minutes", "half_day_min_minutes")):
            default_shift = storage.query_one("SELECT id FROM shifts WHERE is_default = 1")
            if default_shift:
                updates = []
                params = []
                if "shift_start" in changed:
                    updates.append("start_time = ?")
                    params.append(storage.get_setting("shift_start", "09:30"))
                if "shift_end" in changed:
                    updates.append("end_time = ?")
                    params.append(storage.get_setting("shift_end", "18:30"))
                if "late_grace_minutes" in changed:
                    updates.append("grace_minutes = ?")
                    params.append(storage.get_int("late_grace_minutes", 15))
                if "half_day_min_minutes" in changed:
                    updates.append("half_day_minutes = ?")
                    params.append(storage.get_int("half_day_min_minutes", 240))
                if updates:
                    params.append(default_shift["id"])
                    storage.execute(f"UPDATE shifts SET {', '.join(updates)} WHERE id = ?", tuple(params))
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


@router.post("/shifts")
def create_shift(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    name: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    grace_minutes: int = Form(15),
    half_day_minutes: int = Form(240),
):
    verify_form_csrf(user, csrf)
    clean_name = name.strip()
    if not clean_name:
        return redirect("/settings", err="Shift name is required.")
    existing = storage.query_one("SELECT id FROM shifts WHERE lower(name) = lower(?)", (clean_name,))
    if existing:
        return redirect("/settings", err=f"Shift '{clean_name}' already exists.")
    now = utcnow_iso()
    storage.execute(
        "INSERT INTO shifts (name, start_time, end_time, grace_minutes, half_day_minutes, overtime_min_minutes, is_default, created_at) "
        "VALUES (?, ?, ?, ?, ?, 60, 0, ?)",
        (clean_name, start_time.strip(), end_time.strip(), grace_minutes, half_day_minutes, now),
    )
    storage.audit(actor=user.username, action="create_shift", detail=f"Created shift '{clean_name}' ({start_time} - {end_time})")
    return redirect("/settings", ok=f"Shift '{clean_name}' created successfully.")


@router.post("/shifts/{shift_id}/default")
def set_default_shift(
    request: Request,
    shift_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    shift = storage.query_one("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    if not shift:
        return redirect("/settings", err="Shift not found.")
    with storage.transaction():
        storage.execute_tx("UPDATE shifts SET is_default = 0")
        storage.execute_tx("UPDATE shifts SET is_default = 1 WHERE id = ?", (shift_id,))
        # Keep app_settings in sync with the new default shift
        storage.set_setting("shift_start", shift["start_time"])
        storage.set_setting("shift_end", shift["end_time"])
        storage.set_setting("late_grace_minutes", str(shift["grace_minutes"]))
        storage.set_setting("half_day_min_minutes", str(shift["half_day_minutes"]))
    storage.audit(actor=user.username, action="set_default_shift", detail=f"Set shift '{shift['name']}' as default")
    return redirect("/settings", ok=f"Default shift set to '{shift['name']}' ({shift['start_time']} - {shift['end_time']}).")


@router.post("/shifts/{shift_id}/edit")
def edit_shift(
    request: Request,
    shift_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    name: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    grace_minutes: int = Form(15),
    half_day_minutes: int = Form(240),
):
    verify_form_csrf(user, csrf)
    shift = storage.query_one("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    if not shift:
        return redirect("/settings", err="Shift not found.")
    clean_name = name.strip()
    if not clean_name:
        return redirect("/settings", err="Shift name is required.")
    existing = storage.query_one("SELECT id FROM shifts WHERE lower(name) = lower(?) AND id != ?", (clean_name, shift_id))
    if existing:
        return redirect("/settings", err=f"Another shift with name '{clean_name}' already exists.")

    storage.execute(
        "UPDATE shifts SET name = ?, start_time = ?, end_time = ?, grace_minutes = ?, half_day_minutes = ? WHERE id = ?",
        (clean_name, start_time.strip(), end_time.strip(), grace_minutes, half_day_minutes, shift_id),
    )
    if shift["is_default"]:
        storage.set_setting("shift_start", start_time.strip())
        storage.set_setting("shift_end", end_time.strip())
        storage.set_setting("late_grace_minutes", str(grace_minutes))
        storage.set_setting("half_day_min_minutes", str(half_day_minutes))

    storage.audit(actor=user.username, action="edit_shift", detail=f"Updated shift '{clean_name}' ({start_time} - {end_time})")
    return redirect("/settings", ok=f"Shift '{clean_name}' updated successfully.")


@router.post("/shifts/{shift_id}/delete")
def delete_shift(
    request: Request,
    shift_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    shift = storage.query_one("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    if not shift:
        return redirect("/settings", err="Shift not found.")
    if shift["is_default"]:
        return redirect("/settings", err="Cannot delete the default shift.")
    assigned = storage.scalar("SELECT COUNT(*) FROM employees WHERE shift_id = ?", (shift_id,))
    if assigned and assigned > 0:
        return redirect("/settings", err=f"Cannot delete: {assigned} employee(s) are assigned to '{shift['name']}'.")
    storage.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
    storage.audit(actor=user.username, action="delete_shift", detail=f"Deleted shift '{shift['name']}'")
    return redirect("/settings", ok=f"Shift '{shift['name']}' deleted.")


# --------------------------------------------------------------------- WhatsApp (WAHA) Gateway
@router.get("/whatsapp/status")
def whatsapp_status(
    user: SessionUser = Depends(require_hr),
    settings=Depends(get_settings_dep),
):
    info = notify.get_waha_session_info(settings)
    return JSONResponse(info)


@router.get("/whatsapp/qr")
def whatsapp_qr(
    user: SessionUser = Depends(require_hr),
    settings=Depends(get_settings_dep),
):
    png_bytes = notify.get_waha_qr_image(settings)
    if png_bytes:
        return Response(content=png_bytes, media_type="image/png")
    return Response(status_code=204)


@router.post("/whatsapp/start")
def whatsapp_start(
    user: SessionUser = Depends(require_hr),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    res = notify.start_or_restart_waha(settings)
    if res.get("ok"):
        return redirect("/settings#whatsapp", ok="WhatsApp session started. Please scan the QR code.")
    return redirect("/settings#whatsapp", err=f"Failed to start WhatsApp: {res.get('error')}")


@router.post("/whatsapp/logout")
def whatsapp_logout(
    user: SessionUser = Depends(require_hr),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    notify.logout_waha(settings)
    return redirect("/settings#whatsapp", ok="WhatsApp session disconnected successfully.")


@router.post("/whatsapp/test")
def whatsapp_test(
    user: SessionUser = Depends(require_hr),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    phone: str = Form(...),
    message: str = Form(""),
):
    verify_form_csrf(user, csrf)
    res = notify.send_test_whatsapp(settings, phone, message)
    if res.get("ok"):
        return redirect("/settings#whatsapp", ok=f"Test message sent successfully to {phone}!")
    return redirect("/settings#whatsapp", err=f"Failed to send test message: {res.get('error')}")
