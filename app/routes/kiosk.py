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

import hmac
import json
import logging
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import SessionUser, require_hr_or_kiosk
from ..deps import get_face, get_settings_dep, get_storage, render
from ..uploads import UploadError, read_capped
from ..face import FaceServiceError, FaceServiceUnavailable
from .. import notify
from ..services import attendance as attendance_service
from ..services import employees as employees_service
from ..services import orgtime

logger = logging.getLogger("hrms.kiosk")
router = APIRouter(tags=["kiosk"])

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FRAMES = 8

#: After this many spoofs in the window, the device pauses briefly.
SPOOF_LIMIT = 5
SPOOF_WINDOW_SECONDS = 60.0
SPOOF_LOCK_SECONDS = 30.0

RETRY_MESSAGES = {
    "no_face": "No face found — look into the oval.",
    "too_far": "Please step closer.",
    "face_unclear": "Face not clear — look straight at the camera.",
    "too_blurry": "Too blurry — hold still.",
    "too_dark": "Too dark — move to better light.",
    "too_bright": "Too bright — avoid direct light.",
    "look_at_camera": "Please look straight at the camera first.",
    "multiple_faces": "One person at a time, please.",
    "challenge_expired": "That took too long — please try again.",
    "challenge_used": "Please try again.",
    "challenge_invalid": "Please try again.",
    "liveness_error": "Could not check the scan — please try again.",
    "try_again": "Please try again.",
}


def _confirmation(storage, settings) -> str:
    """``full`` or ``anonymous`` (Settings → Gate confirmation card; the old env switch still works)."""
    value = storage.get_setting("kiosk_confirmation", "full")
    if settings.kiosk_show_name:
        return "full"
    return value if value in ("full", "anonymous") else "full"


def _card(employee: dict, outcome, settings) -> dict | None:
    """What the confirmation card shows after a successful punch."""
    if outcome.action not in ("check_in", "check_out"):
        return None
    now = orgtime.local_now(settings)
    stamp = (outcome.check_in if outcome.action == "check_in" else outcome.check_out) or now.strftime("%H:%M:%S")
    clock = orgtime.parse_clock(stamp)
    initials = "".join(part[0] for part in (employee.get("name") or "?").split()[:2]).upper()
    late = ""
    if outcome.action == "check_in" and outcome.late_minutes:
        late = f"Late {outcome.late_minutes} min"
        if outcome.late_half_day:
            late += " · half day"
        elif outcome.late_fine:
            late += f" · ₹{outcome.late_fine:g} fine"
    return {
        "punch": "IN" if outcome.action == "check_in" else "OUT",
        "name": employee.get("name"),
        "code": employee.get("code"),
        "designation": employee.get("designation"),
        "department": employee.get("department"),
        "initials": initials or "?",
        "avatar_url": f"/employees/{employee['id']}/avatar" if employee.get("photo_filename") else None,
        "time": clock.strftime("%I:%M %p").lstrip("0") if clock else stamp,
        "date": now.strftime("%A, %d %b %Y"),
        "late": late,
    }


def device_label_for(user: SessionUser) -> str:
    """Label recorded on scans: the configured device, else a sensible default."""
    if user.device_label:
        return user.device_label
    if user.is_employee:
        return f"Remote ({user.display_name or user.name})"
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
        show_name=_confirmation(storage, settings) == "full",
        scan_mode=storage.get_setting("kiosk_scan_mode", "tap"),
        active_challenge=settings.kiosk_active_challenge,
        offline_enabled=settings.kiosk_offline,
        shift_start=storage.get_setting("shift_start", "09:30"),
        shift_end=storage.get_setting("shift_end", "18:30"),
        debounce_seconds=settings.kiosk_debounce_seconds,
        server_time=orgtime.local_now(settings).strftime("%H:%M:%S"),
        today_label=orgtime.human_day(orgtime.today(settings)),
    )


@router.post("/api/kiosk/challenge")
def kiosk_challenge(
    user: SessionUser = Depends(require_hr_or_kiosk),
    face=Depends(get_face),
):
    """Start a head-turn challenge for the kiosk to show."""
    try:
        return JSONResponse({"ok": True, **face.create_challenge()})
    except FaceServiceError as exc:
        logger.warning("kiosk challenge failed: %s", exc.message)
        return JSONResponse(
            {"ok": False, "action": "service_down", "kind": "error",
             "message": "Face service is unreachable. Attendance cannot be marked right now."},
            status_code=503,
        )


def _read_frames(frames: list[UploadFile]) -> list[bytes]:
    payloads = [read_capped(frame.file, limit=MAX_FRAME_BYTES) for frame in frames[:MAX_FRAMES]]
    if sum(len(payload) for payload in payloads) > MAX_FRAME_BYTES:
        raise UploadError("Frames too large.")
    return payloads


def _spoof_locked(request: Request, device: str) -> bool:
    """Repeated spoofs pause this device for a moment. FaceAPI sees one key, not devices."""
    state = getattr(request.app.state, "kiosk_spoofs", {})
    locked_until = state.get(device, {}).get("locked_until", 0.0)
    return time.monotonic() < locked_until


def _record_spoof(request: Request, device: str) -> None:
    if not hasattr(request.app.state, "kiosk_spoofs"):
        request.app.state.kiosk_spoofs = {}
    entry = request.app.state.kiosk_spoofs.setdefault(device, {"hits": [], "locked_until": 0.0})
    now = time.monotonic()
    entry["hits"] = [hit for hit in entry["hits"] if now - hit < SPOOF_WINDOW_SECONDS] + [now]
    if len(entry["hits"]) >= SPOOF_LIMIT:
        entry["locked_until"] = now + SPOOF_LOCK_SECONDS
        entry["hits"] = []


def _legacy_face_key(identification) -> str | None:
    """Today's rule, kept for ``log`` mode: the top template above the threshold."""
    if identification.external_id:
        return identification.external_id
    if identification.threshold is None:
        return None
    for candidate in identification.candidates or []:
        similarity = candidate.get("similarity")
        if candidate.get("external_id") and similarity is not None and similarity >= identification.threshold:
            return candidate["external_id"]
    return None


def _identify_person(request: Request, storage, settings, user: SessionUser, device: str, identification):
    """Turn a face result into an employee. Returns ``(employee, similarity, problem_response)``."""
    if identification.decision == "retry":
        reason = identification.retry_reason or "try_again"
        return None, None, JSONResponse(
            {
                "ok": False,
                "action": "retry",
                "kind": "warning",
                "reason": reason,
                "message": RETRY_MESSAGES.get(reason, RETRY_MESSAGES["try_again"]),
                "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
            }
        )

    if identification.decision == "spoof" or identification.is_live is False:
        reason = identification.spoof_reason or "liveness_failed"
        _record_spoof(request, device)
        storage.audit(
            actor=user.username,
            action="kiosk_spoof_rejected",
            entity="kiosk",
            detail=f"device={device} reason={reason} score={identification.liveness_score}",
        )
        return None, None, JSONResponse(
            {
                "ok": False,
                "action": "spoof_detected",
                "kind": "error",
                "message": "Liveness check failed. Face the camera yourself, or ask HR.",
                "liveness_score": identification.liveness_score,
                "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
            }
        )

    resolution = employees_service.resolve_face_match(storage, identification)
    enforce = settings.face_template_check == "enforce"
    similarity = resolution.similarity if enforce else identification.similarity
    legacy_key = None if enforce else _legacy_face_key(identification)

    if enforce and resolution.reason == "stale_only":
        storage.audit(actor=user.username, action="kiosk_template_mismatch", entity="kiosk",
                      detail=f"device={device} best={identification.external_id} blocked: stale template")
        return None, None, JSONResponse(
            {"ok": False, "action": "unknown_employee", "kind": "error",
             "message": "This face is enrolled but no longer linked to an employee. Ask HR to re-enrol."},
            status_code=409,
        )

    if (enforce and resolution.employee is None) or (not enforce and not legacy_key):
        no_face = (identification.faces_in_probe or 0) == 0
        message = (
            "No face detected — look straight at the camera."
            if no_face
            else "Face not recognised. Please ask HR to enrol your photo."
        )
        if not no_face:
            storage.audit(actor=user.username, action="kiosk_unknown_face", entity="kiosk",
                          detail=f"device={device} candidates={len(identification.candidates)}")
        return None, None, JSONResponse(
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

    if enforce:
        employee = resolution.employee
    else:
        employee = employees_service.get_employee_by_face_key(storage, legacy_key)
        strict = resolution.employee
        if employee is not None and (strict is None or strict["id"] != employee["id"]):
            # log mode: show what enforce would have done, and change nothing else.
            logger.warning("template check would differ: key=%s reason=%s", legacy_key, resolution.reason)
            storage.audit(actor=user.username, action="kiosk_template_mismatch", entity="employee",
                          entity_id=employee["id"],
                          detail=f"device={device} key={legacy_key} would_block={resolution.reason}")
        if similarity is None and identification.candidates:
            similarity = identification.candidates[0].get("similarity")

    if employee is None:
        storage.audit(actor=user.username, action="kiosk_orphan_template", entity="employee",
                      detail=f"code={legacy_key} is in the face collection but not in HRMS")
        return None, None, JSONResponse(
            {"ok": False, "action": "unknown_employee", "kind": "error",
             "message": "This face is enrolled but no longer linked to an employee. Ask HR to re-enrol."},
            status_code=409,
        )

    return employee, similarity, None


@router.post("/api/kiosk/scan")
def kiosk_scan(
    request: Request,
    user: SessionUser = Depends(require_hr_or_kiosk),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
    image: UploadFile | None = File(None),
    frames: list[UploadFile] = File(default=[]),
    challenge_token: str | None = Form(None),
):
    """Identify the person in the captured frame(s) and mark attendance."""
    device = device_label_for(user)
    if _spoof_locked(request, device):
        return JSONResponse(
            {"ok": False, "action": "spoof_lockout", "kind": "error",
             "message": "Too many failed checks. Please wait a moment or ask HR."},
            status_code=429,
        )

    try:
        if frames and challenge_token:
            frame_payloads = _read_frames(frames)
            payload = frame_payloads[0] if frame_payloads else b""  # capture starts facing the camera
        else:
            frame_payloads = []
            payload = read_capped(image.file, limit=MAX_FRAME_BYTES) if image else b""
    except UploadError:
        return JSONResponse(
            {"ok": False, "action": "frame_too_large", "kind": "error",
             "message": "Frame too large — reduce the camera resolution."},
            status_code=413,
        )

    if not payload:
        return JSONResponse(
            {"ok": False, "action": "empty_frame", "kind": "error", "message": "Empty frame — try again."},
            status_code=400,
        )

    try:
        if frame_payloads:
            identification = face.identify_secure(frame_payloads, challenge_token=challenge_token)
        else:
            identification = face.identify(payload, checks=True)
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

    employee, similarity, problem = _identify_person(request, storage, settings, user, device, identification)
    if problem is not None:
        return problem

    if user.is_employee and user.employee_id and employee["id"] != user.employee_id:
        return JSONResponse(
            {
                "ok": False,
                "action": "identity_mismatch",
                "kind": "error",
                "message": f"Face recognised as {employee.get('name')}, but you are signed in as {user.name}. Please scan your own face.",
            },
            status_code=403,
        )

    outcome = attendance_service.mark_by_face(
        storage,
        settings,
        employee,
        device_label=device,
        similarity=similarity,
        source="face",
        photo_bytes=payload,
    )
    storage.audit(
        actor=user.username, action=f"kiosk_{outcome.action}", entity="employee", entity_id=employee["id"],
        detail=f"{employee['code']} sim={similarity} device={device} live={identification.is_live}",
    )

    _notify_punch(storage, settings, employee, outcome)

    body = outcome.as_dict()
    if _confirmation(storage, settings) == "full":
        body["card"] = _card(employee, outcome, settings)
    else:
        # anonymous gate: nothing about the person leaves the server
        body["employee"] = None
        body["message"] = {
            "check_in": "Checked in.",
            "check_out": "Checked out.",
            "too_early": f"Check-out opens after {outcome.checkout_after}.",
        }.get(outcome.action, "Noted.")
    body.update(
        {
            "similarity": similarity,
            "threshold": identification.threshold,
            "gallery_size": identification.gallery_size,
            "device": device,
            "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
            "today": orgtime.human_day(orgtime.today(settings)),
        }
    )
    return JSONResponse(body)


#: Allowance for a gate clock running slightly ahead of the server.
OFFLINE_CLOCK_SKEW_SECONDS = 300
SCAN_ID = re.compile(r"^[a-f0-9]{16,64}$")
OFFLINE_FINISHED_QUIETLY = {"check_in", "check_out", "duplicate", "already_done", "too_early"}


def _offline_alert(storage, settings, device: str, moment: datetime | None, reason: str) -> None:
    """A person left the gate believing they were marked, so HR has to hear about a failed scan."""
    when = moment.strftime("%d %b %H:%M") if moment else "an unknown time"
    notify.push_hr(storage, settings, "Offline scan needs a check",
                   f"A scan from {device} at {when} was not marked: {reason}. Mark attendance by hand if needed.",
                   "/attendance", category="offline_scan")


@router.post("/api/kiosk/offline-sync")
def kiosk_offline_sync(
    request: Request,
    user: SessionUser = Depends(require_hr_or_kiosk),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
    face=Depends(get_face),
    image: UploadFile = File(...),
    scan_id: str = Form(...),
    captured_at: int = Form(...),
):
    """Apply one scan saved while the gate had no network.

    ``done`` tells the device to delete its copy. A 503 keeps the copy for later. The
    capture time comes from the device, so the record stays marked as offline for HR.
    """
    if not hmac.compare_digest(request.headers.get("x-csrf-token") or "", user.csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    if not settings.kiosk_offline:
        return JSONResponse({"status": "later", "action": "disabled"}, status_code=503)
    if not SCAN_ID.match(scan_id):
        return JSONResponse({"status": "done", "action": "invalid"}, status_code=400)

    device = device_label_for(user)
    seconds = captured_at / 1000
    age = time.time() - seconds
    if age < -OFFLINE_CLOCK_SKEW_SECONDS or age > settings.kiosk_offline_max_hours * 3600:
        storage.audit(actor=user.username, action="kiosk_offline_rejected", entity="kiosk",
                      detail=f"device={device} captured_at={captured_at} age={int(age)}s")
        _offline_alert(storage, settings, device, None, "its time is outside the allowed window")
        return {"status": "done", "action": "too_old"}

    moment = datetime.fromtimestamp(seconds, settings.tz)
    claim_key = f"offline_scan:{moment.date().isoformat()}:{scan_id}"
    if storage.get_setting(claim_key, ""):
        return {"status": "done", "action": "already_synced"}

    try:
        payload = read_capped(image.file, limit=MAX_FRAME_BYTES)
    except UploadError:
        _offline_alert(storage, settings, device, moment, "the photo was too large")
        return {"status": "done", "action": "frame_too_large"}
    if not payload:
        _offline_alert(storage, settings, device, moment, "the photo was empty")
        return {"status": "done", "action": "empty_frame"}

    try:
        identification = face.identify(payload, checks=True)
    except FaceServiceError as exc:
        logger.warning("offline sync waits: %s", exc.message)
        return JSONResponse({"status": "later", "action": "service_down"}, status_code=503)

    employee, similarity, problem = _identify_person(request, storage, settings, user, device, identification)
    if problem is not None:
        action = json.loads(problem.body).get("action") or "not_matched"
        storage.audit(actor=user.username, action="kiosk_offline_not_matched", entity="kiosk",
                      detail=f"device={device} at={moment.isoformat()} action={action}")
        _offline_alert(storage, settings, device, moment, "the face could not be confirmed")
        return {"status": "done", "action": action}

    # One atomic insert, so a retry after a dropped reply cannot mark the same scan twice.
    claimed = storage.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, '1')", (claim_key,))
    if (claimed.rowcount or 0) != 1:
        return {"status": "done", "action": "already_synced"}
    try:
        outcome = attendance_service.mark_offline_scan(
            storage, settings, employee, moment=moment, device_label=device, similarity=similarity, photo_bytes=payload,
        )
    except Exception:
        storage.execute("DELETE FROM app_settings WHERE key = ?", (claim_key,))  # let the device try again
        raise
    storage.audit(
        actor=user.username, action=f"kiosk_offline_{outcome.action}", entity="employee", entity_id=employee["id"],
        detail=f"{employee['code']} at={moment.isoformat()} sim={similarity} device={device} live={identification.is_live}",
    )
    notify.push_punch(storage, settings, employee, outcome)
    if outcome.action not in OFFLINE_FINISHED_QUIETLY:
        _offline_alert(storage, settings, device, moment, f"{employee['name']}: {outcome.message}")
    return {"status": "done", "action": outcome.action}


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
    if user.is_kiosk:
        attendance_service.record_kiosk_heartbeat(storage, device_label_for(user))
    payload: dict = {
        "face_service": {
            "ok": bool(health.get("ok")),
            "detail": health.get("detail"),
            "collection": health.get("collection"),
        },
        "server_time": orgtime.local_now(settings).strftime("%H:%M:%S"),
        "server_epoch_ms": int(time.time() * 1000),
        "csrf": user.csrf,
        "today": orgtime.human_day(orgtime.today(settings)),
        "device": device_label_for(user),
        "show_name": _confirmation(storage, settings) == "full",
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
    notify.push_punch(storage, settings, employee, outcome)
    is_in = outcome.action == "check_in"
    if is_in and not settings.notify_punch_in and not (outcome.late_minutes and settings.notify_late):
        return

    template = "punch_in" if is_in else "check_out"
    ctx = {
        "name": (employee.get("name") or "").split(" ")[0] or "there",
        "time": (outcome.check_in if is_in else outcome.check_out) or "",
        "date": orgtime.today_str(settings),
        "late_minutes": outcome.late_minutes,
        "shift_start": orgtime.shift_for(storage, employee)["start_time"],
        "worked": orgtime.format_minutes(outcome.worked_minutes),
    }
    wa_att_val = storage.get_setting("notify_whatsapp_attendance")
    wa_attendance_enabled = settings.notify_whatsapp_attendance if wa_att_val is None else (str(wa_att_val).strip() in ("1", "true", "True", "yes"))
    whatsapp_sent = False
    if wa_attendance_enabled and employee.get("phone"):
        notify.queue_whatsapp(
            storage, employee["phone"], template, settings=settings, **ctx
        )
        whatsapp_sent = True

    # Decide whether to email: send on check-out, on normal punch-in (if enabled),
    # or on late punch-in (if either punch-in or late notifications are enabled).
    # When WhatsApp attendance notification is active and sent to employee,
    # skip sending email as requested ("jab whatsapp me checkin checkout ka notification employee ko ja rha hai to iske liye email mat bhejo").
    should_email = False
    if not whatsapp_sent:
        if is_in:
            should_email = settings.notify_punch_in or (bool(outcome.late_minutes) and settings.notify_late)
        else:
            should_email = settings.notify_punch_out

        if settings.notify_email_enabled and employee.get("email") and should_email:
            category = "late_alert" if (is_in and outcome.late_minutes) else f"attendance_{template}"
            notify.queue_email(
                storage, employee["email"], template, settings=settings,
                category=category, **ctx,
            )
