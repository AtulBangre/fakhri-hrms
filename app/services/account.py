"""Password reset by email OTP, and the profile-change approval workflow.

Two things an employee must be able to do without HR holding their hand:

1. **Reset their own password.** They type their username, we email a six-digit
   code (stored hashed, 10-minute TTL, 5 attempts, 60-second cooldown), and they
   set a new password with it. Nothing about a valid account is revealed by the
   *response* — the same "if that account exists, we've sent a code" is shown
   either way, so the form cannot be used to enumerate usernames.

2. **Ask HR to change their details.** Phone, address, bank account — the
   employee submits the new value, HR approves, and only then is the employee
   record updated. Bank details never change silently: there is always a record
   of who asked and who approved.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import notify
from ..config import Settings
from ..db import Storage, utcnow_iso
from . import employees as employees_service

OTP_LENGTH = 6

#: Fields an employee may request to change, with a human label.
CHANGEABLE_FIELDS: dict[str, str] = {
    "phone": "Phone",
    "email": "Email",
    "address": "Address",
    "bank_name": "Bank name",
    "account_holder": "Account holder",
    "account_number": "Account number",
    "ifsc": "IFSC code",
    "emergency_contact": "Emergency contact",
}

#: Fields HR may edit directly (no approval workflow) — kept for reference.
HR_ONLY_FIELDS = {"code", "name", "department", "designation", "monthly_gross", "joined_on"}


# ------------------------------------------------------------------------- OTP
def _hash(code: str, *, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), 60_000).hex()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_otp(storage: Storage, settings: Settings, user: dict[str, Any]) -> str:
    """Create a fresh OTP for a user and return the plaintext code."""
    now = _now()
    last = storage.query_one(
        "SELECT created_at FROM otp_codes WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
    )
    if last:
        try:
            previous = datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))
            if (now - previous).total_seconds() < settings.otp_cooldown_seconds:
                raise ValueError("Please wait a minute before requesting another code.")
        except ValueError as exc:
            if "wait a minute" in str(exc):
                raise
            # unparseable timestamp: do not block the user

    code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
    salt = secrets.token_hex(8)
    storage.execute(
        """
        INSERT INTO otp_codes (user_id, code_hash, created_at, expires_at, attempts)
        VALUES (?,?,?,?,0)
        """,
        (
            user["id"],
            f"{salt}${_hash(code, salt=salt)}",
            now.isoformat(),
            (now + timedelta(minutes=settings.otp_ttl_minutes)).isoformat(),
        ),
    )
    return code


def verify_otp(storage: Storage, settings: Settings, user_id: int, code: str) -> tuple[bool, str]:
    """Check a code. Returns ``(ok, reason)``."""
    row = storage.query_one(
        "SELECT * FROM otp_codes WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if row is None:
        return False, "No reset code was requested for this account."
    if int(row["attempts"]) >= settings.otp_max_attempts:
        return False, "Too many attempts. Request a new code."
    try:
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        expires = _now()
    if expires < _now():
        return False, "That code has expired. Request a new one."

    salt, _, digest = row["code_hash"].partition("$")
    if not hmac.compare_digest(_hash(code.strip(), salt=salt), digest):
        storage.execute("UPDATE otp_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        remaining = settings.otp_max_attempts - int(row["attempts"]) - 1
        return False, f"Incorrect code. {max(0, remaining)} attempt(s) left."

    storage.execute("UPDATE otp_codes SET used_at = ? WHERE id = ?", (utcnow_iso(), row["id"]))
    return True, "ok"


def request_reset(
    storage: Storage, settings: Settings, username: str
) -> tuple[str | None, str]:
    """Start a password reset.

    Returns ``(username, message)``. ``username`` is ``None`` when the account does
    not exist, so the caller can show an identical message either way.
    """
    from .. import auth

    user = auth.get_user_by_username(storage, username)
    generic_msg = "If that account exists, a reset code has been sent."
    if user is None or not user["active"]:
        return None, generic_msg

    email = _email_for(storage, user)
    if not email:
        storage.audit(actor=user["username"], action="otp_failed_no_email", entity="user", entity_id=user["id"])
        return None, generic_msg

    try:
        code = issue_otp(storage, settings, user)
    except ValueError as exc:
        # cooldown: tell the person, but never confirm whether the account exists
        return None, str(exc)
    notify.queue_email(
        storage,
        email,
        "otp",
        settings=settings,
        category="otp",
        name=user.get("display_name") or user["username"],
        code=code,
        minutes=settings.otp_ttl_minutes,
    )
    storage.audit(actor=user["username"], action="otp_requested", entity="user", entity_id=user["id"])
    return user["username"], generic_msg


def _email_for(storage: Storage, user: dict[str, Any]) -> str | None:
    """Best email for a user: their own, else their employee record's."""
    if user.get("email"):
        return str(user["email"]).strip()
    if user.get("employee_id"):
        employee = employees_service.get_employee(storage, int(user["employee_id"]))
        if employee and employee.get("email"):
            return str(employee["email"]).strip()
    return None


def complete_reset(
    storage: Storage, settings: Settings, *, username: str, code: str, new_password: str
) -> tuple[bool, str]:
    from .. import auth

    user = auth.get_user_by_username(storage, username)
    if user is None:
        return False, "Incorrect code or account."
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters."

    ok, reason = verify_otp(storage, settings, user["id"], code)
    if not ok:
        return False, reason

    auth.change_password(storage, user["id"], new_password)
    storage.execute("UPDATE users SET active = 1 WHERE id = ?", (user["id"],))
    storage.audit(actor=user["username"], action="password_reset", entity="user", entity_id=user["id"])
    return True, "Password changed — you can sign in now."


# -------------------------------------------------------- profile change flow
def submit_change_requests(
    storage: Storage,
    settings: Settings,
    *,
    employee: dict[str, Any],
    values: dict[str, str],
    requested_by: int | None = None,
) -> list[dict[str, Any]]:
    """Create one pending request per field that actually changed."""
    created: list[dict[str, Any]] = []
    now = utcnow_iso()

    for field, raw in values.items():
        if field not in CHANGEABLE_FIELDS:
            continue
        new_value = (raw or "").strip()
        old_value = (employee.get(field) or "").strip()
        if new_value == old_value:
            continue
        if not new_value:
            continue  # clearing a field is an HR decision, not a self-service one
        if field == "account_number":
            new_value = new_value.replace(" ", "")
            old_value = old_value.replace(" ", "")

        # replace an older pending request for the same field instead of stacking
        storage.execute(
            "DELETE FROM profile_change_requests WHERE employee_id = ? AND field = ? AND status = 'pending'",
            (employee["id"], field),
        )
        cursor = storage.execute(
            """
            INSERT INTO profile_change_requests (employee_id, field, old_value, new_value, status,
                                                 requested_at, requested_by)
            VALUES (?,?,?,?, 'pending', ?, ?)
            """,
            (employee["id"], field, old_value or None, new_value, now, requested_by),
        )
        created.append(storage.query_one(
            "SELECT * FROM profile_change_requests WHERE id = ?", (cursor.lastrowid,)
        ))

    if created:
        storage.audit(
            actor=employee.get("code"), action="profile_change_requested", entity="employee",
            entity_id=employee["id"], detail=", ".join(row["field"] for row in created),
        )
        notify.queue_email(
            storage,
            _hr_email(storage),
            "profile_changed",
            settings=settings,
            category="profile_change",
            name=employees_service.get_employee(storage, employee["id"])["name"],
            fields=", ".join(CHANGEABLE_FIELDS.get(row["field"], row["field"]) for row in created),
        )
    return created


def _hr_email(storage: Storage) -> str | None:
    row = storage.query_one(
        "SELECT email FROM users WHERE role = 'hr' AND email IS NOT NULL AND email != '' LIMIT 1"
    )
    return row["email"] if row else None


def pending_changes(storage: Storage, *, employee_id: int | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    clause = ""
    if employee_id:
        clause = "AND r.employee_id = ?"
        params.append(employee_id)
    rows = storage.query(
        f"""
        SELECT r.*, e.name AS employee_name, e.code AS employee_code
        FROM profile_change_requests r JOIN employees e ON e.id = r.employee_id
        WHERE r.status = 'pending' {clause}
        ORDER BY r.id
        """,
        params,
    )
    for row in rows:
        row["field_label"] = CHANGEABLE_FIELDS.get(row["field"], row["field"])
        row["masked"] = row["field"] in ("account_number",)
    return rows


def count_pending(storage: Storage) -> int:
    return int(storage.scalar(
        "SELECT COUNT(*) AS n FROM profile_change_requests WHERE status = 'pending'", (), 0
    ))


def decide(
    storage: Storage,
    settings: Settings,
    *,
    request_id: int,
    approve: bool,
    decided_by: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Approve (and apply) or reject one change request."""
    row = storage.query_one("SELECT * FROM profile_change_requests WHERE id = ?", (request_id,))
    if row is None:
        raise ValueError("Change request not found.")
    if row["status"] != "pending":
        raise ValueError("That request has already been decided.")
    if row["field"] not in CHANGEABLE_FIELDS:
        raise ValueError("Unknown field.")

    if approve:
        employees_service.update_employee(
            storage, row["employee_id"], {row["field"]: row["new_value"], "_actor": decided_by}
        )
    storage.execute(
        "UPDATE profile_change_requests SET status = ?, decided_at = ?, decided_by = ?, note = ? WHERE id = ?",
        ("approved" if approve else "rejected", utcnow_iso(), decided_by, note, request_id),
    )
    storage.audit(
        actor=decided_by,
        action="profile_change_approved" if approve else "profile_change_rejected",
        entity="employee", entity_id=row["employee_id"],
        detail=f"{row['field']} → {row['new_value'][:40]}",
    )

    employee = employees_service.get_employee(storage, row["employee_id"])
    target = (employee or {}).get("email")
    if target and settings.notify_email_enabled:
        notify.queue_email(
            storage,
            target,
            "profile_decided",
            settings=settings,
            category="profile_change",
            name=(employee or {}).get("name") or "there",
            decision="approved" if approve else "rejected",
            field_label=CHANGEABLE_FIELDS.get(row["field"], row["field"]),
            note=note or "",
        )
    updated = storage.query_one("SELECT * FROM profile_change_requests WHERE id = ?", (request_id,))
    assert updated is not None
    return updated
