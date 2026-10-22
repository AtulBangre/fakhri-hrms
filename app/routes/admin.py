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
):
    verify_form_csrf(user, csrf)
    allowed = {definition[0] for definition in SETTING_DEFS}
    if key not in allowed:
        return redirect("/settings", err=f"Unknown setting '{key}'.")
    storage.set_setting(key, value.strip())
    storage.audit(actor=user.username, action="update_setting", entity="settings", entity_id=key,
                  detail=value.strip()[:120])
    return redirect("/settings", ok=f"Setting saved: {key}.")


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
):
    verify_form_csrf(user, csrf)
    if role not in (auth.ROLE_HR, auth.ROLE_KIOSK):
        return redirect("/settings", err="Role must be hr or kiosk.")
    if len(password) < 8:
        return redirect("/settings", err="Password must be at least 8 characters.")
    if auth.get_user_by_username(storage, username):
        return redirect("/settings", err=f"Username '{username}' already exists.")
    create_user(
        storage, username=username, password=password, role=role,
        display_name=display_name or username, device_label=device_label or None,
    )
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
