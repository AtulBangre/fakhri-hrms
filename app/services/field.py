"""Remote / field attendance with GPS geo-fencing.

Field staff cannot scan a gate, so they check in from their phone: the browser
sends coordinates, the server measures the distance to every configured work
location, and attendance is only marked when the person is **inside** one of the
allowed circles.

Honest limitation, stated up front: a browser's GPS can be spoofed (developer
tools, mock-location apps). Geo-fencing raises the effort, it does not prove
presence. That is why every field check stores the coordinates, the accuracy, the
distance and an optional selfie — HR can review the evidence instead of trusting a
single "present" flag.
"""

from __future__ import annotations

import math
from typing import Any

from ..config import Settings
from ..db import Storage, utcnow_iso
from ..uploads import delete_file, save_upload
from . import attendance as attendance_service
from . import employees as employees_service
from . import orgtime

EARTH_RADIUS_M = 6_371_000


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# ------------------------------------------------------------------ locations
def add_location(
    storage: Storage,
    *,
    name: str,
    lat: float,
    lng: float,
    radius_m: int | None = None,
    address: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the location a name (e.g. 'Client site — Arera Colony').")
    if not (-90 <= float(lat) <= 90 and -180 <= float(lng) <= 180):
        raise ValueError("Those coordinates are not valid.")
    radius = int(radius_m or storage.get_int("geofence_radius_meters", 200))
    if radius < 25:
        raise ValueError("The radius must be at least 25 metres.")
    cursor = storage.execute(
        "INSERT INTO work_locations (name, address, lat, lng, radius_m, active, created_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (name[:120], address, float(lat), float(lng), radius, utcnow_iso()),
    )
    storage.audit(actor=actor, action="work_location_added", entity="work_location",
                  entity_id=cursor.lastrowid, detail=name)
    return get_location(storage, int(cursor.lastrowid))


def get_location(storage: Storage, location_id: int) -> dict[str, Any]:
    row = storage.query_one("SELECT * FROM work_locations WHERE id = ?", (location_id,))
    if row is None:
        raise ValueError("Location not found.")
    return row


def list_locations(storage: Storage, *, active_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE active = 1" if active_only else ""
    return storage.query(f"SELECT * FROM work_locations {where} ORDER BY name")


def toggle_location(storage: Storage, location_id: int, *, actor: str | None = None) -> dict[str, Any]:
    row = get_location(storage, location_id)
    storage.execute("UPDATE work_locations SET active = ? WHERE id = ?", (0 if row["active"] else 1, location_id))
    storage.audit(actor=actor, action="work_location_toggled", entity="work_location", entity_id=location_id)
    return get_location(storage, location_id)


def delete_location(storage: Storage, location_id: int, *, actor: str | None = None) -> None:
    storage.execute("DELETE FROM work_locations WHERE id = ?", (location_id,))
    storage.audit(actor=actor, action="work_location_deleted", entity="work_location", entity_id=location_id)


def nearest(storage: Storage, lat: float, lng: float) -> tuple[dict[str, Any] | None, float | None]:
    """Closest active location and the distance to it."""
    best, best_distance = None, None
    for row in list_locations(storage, active_only=True):
        distance = haversine_m(lat, lng, float(row["lat"]), float(row["lng"]))
        if best_distance is None or distance < best_distance:
            best, best_distance = row, distance
    return best, best_distance


# ---------------------------------------------------------------- field check
def check_in(
    storage: Storage,
    settings: Settings,
    *,
    employee: dict[str, Any],
    lat: float,
    lng: float,
    accuracy: float | None = None,
    selfie: tuple[bytes, str] | None = None,
    kind: str = "check_in",
    note: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Validate the geo-fence and, if inside, mark attendance.

    Returns a dict with ``ok``, ``inside``, ``distance_m``, ``location``,
    ``message`` and the attendance outcome when it was marked.
    """
    if kind not in ("check_in", "check_out"):
        raise ValueError("Unknown field check type.")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise ValueError("The browser did not send valid coordinates.") from None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("Those coordinates are not valid.")

    mode = employees_service.self_punch_mode(storage, employee)
    if mode == "disabled":
        return {
            "ok": False, "inside": False, "distance_m": None, "location": None,
            "message": "Attendance is marked at the gate scanner only. Ask HR if you need to punch from your phone.",
        }

    locations = list_locations(storage, active_only=True)
    if not locations and mode != "always_allowed":
        return {
            "ok": False, "inside": False, "distance_m": None, "location": None,
            "message": "HR has not configured any work location yet — ask them to add one before using field attendance.",
        }

    if locations:
        location, distance = nearest(storage, lat, lng)
        assert location is not None and distance is not None
        inside = distance <= float(location["radius_m"])
    else:
        location, distance, inside = None, None, False
    # "always_allowed": anywhere counts; the position is still recorded for HR (Feature 3)
    allowed = inside or mode == "always_allowed"
    dist = round(distance, 1) if distance is not None else None
    place = location["name"] if (location and inside) else "Remote"

    selfie_name = None
    if selfie:
        data, original = selfie
        stored = save_upload(settings, "selfies", data, original_name=original)
        selfie_name = stored.stored_name

    now = orgtime.local_now(settings)
    row_id = None
    try:
        cursor = storage.execute(
            """
            INSERT INTO field_checks (employee_id, work_date, kind, lat, lng, accuracy_m, distance_m,
                                      location_id, location_name, inside, selfie_name, note, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                employee["id"], now.date().isoformat(), kind, lat, lng, accuracy, dist,
                location["id"] if location else None, location["name"] if location else None,
                1 if inside else 0, selfie_name, note, utcnow_iso(),
            ),
        )
        row_id = int(cursor.lastrowid)
    except Exception:  # pragma: no cover - keep the file from leaking on a DB error
        delete_file(settings, "selfies", selfie_name)
        raise

    # A reading less precise than the fence itself cannot prove where someone is (ATT-05).
    if mode == "geofence_only" and inside and accuracy is not None and float(accuracy) > float(location["radius_m"]):
        storage.execute("UPDATE field_checks SET inside = 0, note = COALESCE(note, 'weak gps') WHERE id = ?", (row_id,))
        return {
            "ok": False, "inside": False, "distance_m": dist, "location": location,
            "message": f"GPS signal too weak (±{float(accuracy):.0f} m) — move outdoors or near a window and retry.",
            "check_id": row_id,
        }
    if allowed and kind == "check_out":
        today_row = attendance_service.get_row(storage, employee["id"], now.date())
        if not today_row or not today_row.get("check_in"):
            return {
                "ok": False, "inside": inside, "distance_m": dist, "location": location,
                "message": "You haven't checked in today — choose Check in first.",
                "check_id": row_id,
            }

    if not allowed:
        storage.audit(actor=actor, action="field_check_outside", entity="employee", entity_id=employee["id"],
                      detail=f"{distance:.0f}m from {location['name']}")
        return {
            "ok": False, "inside": False, "distance_m": dist,
            "location": location,
            "message": (
                f"You are {distance / 1000:.2f} km from {location['name']} "
                f"(allowed {location['radius_m']} m). Attendance was not marked."
            ),
            "check_id": row_id,
        }

    outcome = attendance_service.mark_by_face(
        storage, settings, employee,
        device_label=f"Field · {place}",
        moment=now,
        source="field",
    )
    storage.audit(actor=actor, action="field_check_inside", entity="employee",
                  entity_id=employee["id"],
                  detail=f"{kind} at {place}" + (f" ({distance:.0f}m)" if distance is not None else ""))
    storage.execute(
        "UPDATE field_checks SET note = COALESCE(note, ?) WHERE id = ?",
        (outcome.action, row_id),
    )
    return {
        "ok": outcome.ok,
        "inside": inside,
        "distance_m": dist,
        "location": location,
        "message": outcome.message,
        "check_id": row_id,
        "attendance": outcome.as_dict(),
    }


def checks_for(storage: Storage, *, employee_id: int | None = None, day: str | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
    clauses, params = [], []
    if employee_id:
        clauses.append("f.employee_id = ?")
        params.append(employee_id)
    if day:
        clauses.append("f.work_date = ?")
        params.append(day)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return storage.query(
        f"""
        SELECT f.*, e.name AS employee_name, e.code AS employee_code
        FROM field_checks f JOIN employees e ON e.id = f.employee_id
        {where} ORDER BY f.id DESC LIMIT ?
        """,
        params,
    )


def today_summary(storage: Storage, settings: Settings) -> dict[str, Any]:
    today = orgtime.today_str(settings)
    rows = storage.query(
        "SELECT inside, COUNT(*) AS n FROM field_checks WHERE work_date = ? GROUP BY inside", (today,)
    )
    totals = {int(row["inside"]): int(row["n"]) for row in rows}
    marked = int(storage.scalar(
        "SELECT COUNT(*) AS n FROM attendance WHERE work_date = ? AND source = 'field'", (today,), 0
    ))
    return {"inside": totals.get(1, 0), "outside": totals.get(0, 0), "attendance_marked": marked}
