"""The gate kiosk: a full-screen face scanner that can *only* mark attendance.

Access model
------------
* Both HR and kiosk logins may open ``/kiosk``, so HR can test the scanner.
* The kiosk role cannot reach any other page (``require_hr`` redirects it back),
  cannot see salaries, and cannot correct attendance — a stolen gate phone is
  therefore not an HR data leak.
* Every scan is rate-limited by the debounce window in the attendance service, so
  queueing people in front of the camera cannot corrupt anybody's hours.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import SessionUser, require_hr_or_kiosk
from ..deps import get_face, get_settings_dep, get_storage, render
from ..face import FaceServiceError, FaceServiceUnavailable
from .. import notify
from ..services import attendance as attendance_service
from ..services import employees as employees_service
from ..services import orgtime

logger = logging.getLogger("hrms.kiosk")
router = APIRouter(tags=["kiosk"])

MAX_FRAME_BYTES = 8 * 1024 * 1024


def device_label_for(user: SessionUser) -> str:
    """Label recorded on scans: the configured device, else a sensible default."""
    if user.device_label:
        return user.device_label
    return "Gate kiosk" if user.is_kiosk else "HR console"


@router.get("/kiosk")
def kiosk_screen(
    request: Request,
    user: SessionUser = Depends(require_hr_or_kiosk),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
):
    health = face.health()
    return render(
        request,
        "kiosk.html",
        active="kiosk",
        user=user,
        device_label=device_label_for(user),
        health=health,
        show_name=settings.kiosk_show_name,
        shift_start=storage.get_setting("shift_start", "09:30"),
        debounce_seconds=settings.kiosk_debounce_seconds,
        server_time=orgtime.local_now(settings).strftime("%H:%M:%S"),
        today_label=orgtime.human_day(orgtime.today(settings)),
    )


@router.post("/api/kiosk/scan")
def kiosk_scan(
    request: Request,
    user: SessionUser = Depends(require_hr_or_kiosk),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
    image: UploadFile = File(...),
):
    """Identify the person in a captured frame and mark attendance."""
    payload = image.file.read()
    device = device_label_for(user)

    if not payload:
        return JSONResponse(
            {"ok": False, "action": "empty_frame", "kind": "error", "message": "Empty frame — try again."},
            status_code=400,
        )
    if len(payload) > MAX_FRAME_BYTES:
        return JSONResponse(
            {"ok": False, "action": "frame_too_large", "kind": "error",
             "message": "Frame too large — reduce the camera resolution."},
            status_code=413,
        )

    try:
        identification = face.identify(payload)
    except FaceServiceUnavailable as exc:
        logger.warning("kiosk scan failed: face service unavailable (%s)", exc)
        return JSONResponse(
            {"ok": False, "action": "service_down", "kind": "error",
             "message": "Face service is unreachable. Attendance cannot be marked right now."},
            status_code=503,
        )
    except FaceServiceError as exc:
        logger.warning("kiosk scan rejected by face service: %s", exc.message)
        return JSONResponse(
            {"ok": False, "action": "service_error", "kind": "error", "message": exc.message},
            status_code=502,
        )

    if not identification.matched or not identification.external_id:
        no_face = (identification.faces_in_probe or 0) == 0
        message = (
            "No face detected — look straight at the camera."
            if no_face
            else "Face not recognised. Please ask HR to enrol your photo."
        )
        if not no_face:
            storage.audit(actor=user.username, action="kiosk_unknown_face", entity="kiosk",
                          detail=f"device={device} candidates={len(identification.candidates)}")
        return JSONResponse(
            {
                "ok": False,
                "action": "no_face" if no_face else "unknown_face",
                "kind": "warning" if no_face else "error",
                "message": message,
                "similarity": identification.similarity,
                "faces_in_probe": identification.faces_in_probe,
                "threshold": identification.threshold,
                "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
            }
        )

    employee = employees_service.get_employee_by_code(storage, identification.external_id)
    if employee is None:
        storage.audit(actor=user.username, action="kiosk_orphan_template", entity="employee",
                      detail=f"code={identification.external_id} is in the face collection but not in HRMS")
        return JSONResponse(
            {"ok": False, "action": "unknown_employee", "kind": "error",
             "message": "This face is enrolled but no longer linked to an employee. Ask HR to re-enrol."},
            status_code=409,
        )

    outcome = attendance_service.mark_by_face(
        storage,
        settings,
        employee,
        device_label=device,
        similarity=identification.similarity,
        source="face",
    )
    storage.audit(
        actor=user.username, action=f"kiosk_{outcome.action}", entity="employee", entity_id=employee["id"],
        detail=f"{employee['code']} sim={identification.similarity} device={device}",
    )

    _notify_punch(storage, settings, employee, outcome)

    body = outcome.as_dict()
    body.update(
        {
            "similarity": identification.similarity,
            "threshold": identification.threshold,
            "gallery_size": identification.gallery_size,
            "device": device,
            "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
            "today": orgtime.human_day(orgtime.today(settings)),
        }
    )
    return JSONResponse(body)


@router.get("/api/kiosk/status")
def kiosk_status(
    request: Request,
    user: SessionUser = Depends(require_hr_or_kiosk),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
):
    """Scanner health for the gate screen.

    The kiosk role gets **only** the service state and the clock — no counters and
    no list of who has scanned, because that screen is shared and a queue of
    colleagues must not be able to see each other's attendance. HR (which may open
    the scanner for testing) still gets the full summary for monitoring.
    """
    health = face.health()
    payload: dict = {
        "face_service": {
            "ok": bool(health.get("ok")),
            "detail": health.get("detail"),
            "collection": health.get("collection"),
        },
        "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
        "today": orgtime.human_day(orgtime.today(settings)),
        "device": device_label_for(user),
        "show_name": settings.kiosk_show_name,
    }

    if user.is_hr:
        board = attendance_service.dashboard(storage, settings)
        payload["counts"] = {
            "present": board["counts"]["present"],
            "half_day": board["counts"]["half_day"],
            "late": board["counts"]["late"],
            "on_leave": board["counts"]["leave"],
            "absent": board["counts"]["absent"],
            "not_marked": board["counts"]["not_marked"],
            "inside_now": int(
                storage.scalar(
                    "SELECT COUNT(*) AS n FROM attendance WHERE work_date = ? "
                    "AND check_in IS NOT NULL AND check_out IS NULL",
                    (orgtime.today_str(settings),),
                )
            ),
            "headcount": board["counts"]["total"],
        }
        payload["recent"] = [
            {
                "name": row["name"],
                "code": row["code"],
                "check_in": row["check_in_label"],
                "check_out": row["check_out_label"],
                "status": row["status"],
            }
            for row in attendance_service.recent_scans(storage, limit=6)
        ]

    return JSONResponse(payload)


def _notify_punch(storage, settings, employee: dict, outcome) -> None:
    """Queue the punch-in / check-out confirmation (and a late warning).

    Fire-and-forget on purpose: the queue write is a single insert, so the gate
    never waits for SMTP. Nothing is sent if the employee has no contact details.
    """
    if outcome.action not in ("check_in", "check_out"):
        return
    is_in = outcome.action == "check_in"
    if is_in and not settings.notify_punch_in:
        return

    template = "punch_in" if is_in else "check_out"
    ctx = {
        "name": (employee.get("name") or "").split(" ")[0] or "there",
        "time": (outcome.check_in if is_in else outcome.check_out) or "",
        "date": orgtime.today_str(settings),
        "late_minutes": outcome.late_minutes,
        "shift_start": storage.get_setting("shift_start", "09:30"),
        "worked": orgtime.format_minutes(outcome.worked_minutes),
    }
    if settings.notify_email_enabled and employee.get("email"):
        notify.queue_email(
            storage, employee["email"], template, settings=settings,
            category=f"attendance_{template}", **ctx,
        )
    if settings.notify_whatsapp_attendance and employee.get("phone"):
        notify.queue_whatsapp(
            storage, employee["phone"], template, settings=settings, **ctx
        )
    # an explicit late alert, independent of the punch-in confirmation
    if is_in and outcome.late_minutes and settings.notify_late and settings.notify_email_enabled:
        if employee.get("email"):
            notify.queue_email(
                storage, employee["email"], "punch_in", settings=settings,
                category="late_alert", **ctx,
            )
