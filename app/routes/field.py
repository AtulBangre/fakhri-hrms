"""Field attendance: HR sets the allowed locations, staff check in from a phone."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import SessionUser, require_employee, require_hr, verify_form_csrf
from ..deps import get_settings_dep, get_storage, redirect, render
from ..uploads import read_capped
from ..services import employees as employees_service
from ..services import field as field_service
from ..services import orgtime

router = APIRouter(tags=["field"])


@router.get("/locations")
def locations_page(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    return render(
        request, "locations.html", active="locations", user=user,
        rows=field_service.list_locations(storage),
        checks=field_service.checks_for(storage, day=orgtime.today_str(settings), limit=20),
        summary=field_service.today_summary(storage, settings),
        default_radius=storage.get_int("geofence_radius_meters", 200),
        today=orgtime.today_str(settings),
    )


@router.post("/locations")
def add_location(
    request: Request,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
    name: str = Form(...),
    lat: str = Form(...),
    lng: str = Form(...),
    radius_m: str = Form(""),
    address: str = Form(""),
):
    verify_form_csrf(user, csrf)
    try:
        field_service.add_location(
            storage, name=name, lat=float(lat), lng=float(lng),
            radius_m=int(radius_m) if radius_m.strip() else None,
            address=address or None, actor=user.username,
        )
    except (ValueError, TypeError) as exc:
        return redirect("/locations", err=str(exc))
    return redirect("/locations", ok=f"Location '{name}' added.")


@router.post("/locations/{location_id}/toggle")
def toggle_location(
    request: Request,
    location_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    row = field_service.toggle_location(storage, location_id, actor=user.username)
    return redirect("/locations", ok="Location enabled." if row["active"] else "Location disabled.")


@router.post("/locations/{location_id}/delete")
def delete_location(
    request: Request,
    location_id: int,
    user: SessionUser = Depends(require_hr),
    storage=Depends(get_storage),
    csrf: str = Form("", alias="_csrf"),
):
    verify_form_csrf(user, csrf)
    field_service.delete_location(storage, location_id, actor=user.username)
    return redirect("/locations", ok="Location removed.")


# --------------------------------------------------------------- employee side
@router.get("/me/field")
def my_field(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))
    return render(
        request, "me_field.html", active="me-field", user=user, employee=employee,
        locations=field_service.list_locations(storage, active_only=True),
        checks=field_service.checks_for(storage, employee_id=employee["id"], limit=10),
        today=orgtime.today_str(settings),
        default_radius=storage.get_int("geofence_radius_meters", 200),
        punch_mode=employees_service.self_punch_mode(storage, employee),
    )


@router.post("/api/field/check")
def field_check_api(
    request: Request,
    user: SessionUser = Depends(require_employee),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    csrf: str = Form("", alias="_csrf"),
    lat: str = Form(...),
    lng: str = Form(...),
    accuracy: str = Form(""),
    kind: str = Form("check_in"),
    note: str = Form(""),
    selfie: UploadFile | None = File(None),
):
    """JSON endpoint used by the field page (so the result can be shown inline)."""
    verify_form_csrf(user, csrf)
    employee = employees_service.require_employee(storage, int(user.employee_id or 0))
    payload = None
    try:
        if selfie is not None and selfie.filename:
            payload = (read_capped(selfie.file), selfie.filename)
        result = field_service.check_in(
            storage, settings, employee=employee, lat=float(lat), lng=float(lng),
            accuracy=float(accuracy) if accuracy.strip() else None,
            selfie=payload, kind=kind, note=note or None, actor=user.username,
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "inside": False, "message": str(exc)}, status_code=400)

    location = result.get("location") or {}
    body = {
        "ok": result["ok"],
        "inside": result["inside"],
        "message": result["message"],
        "distance_m": result.get("distance_m"),
        "location": {"name": location.get("name"), "radius_m": location.get("radius_m")} if location else None,
        "attendance": result.get("attendance"),
    }
    return JSONResponse(body, status_code=200 if (result.get("inside") or result.get("ok")) else 422)
