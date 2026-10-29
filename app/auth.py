"""Authentication: HR and kiosk roles, password hashing, sessions and CSRF.

Design notes
------------
* Passwords are hashed with **scrypt** (stdlib ``hashlib``), salted per user, so
  there is no extra dependency and no plaintext anywhere on disk.
* Sessions are **stateless signed cookies** (HMAC-SHA256 over a base64 payload).
  The payload carries the user id, role and a CSRF token; it is signed, not
  encrypted, so it deliberately holds no secrets.
* Two roles exist:
    ``hr``     — full access to people, attendance, leave and payroll.
    ``kiosk``  — the gate device: it may only open the scanner and mark
                 attendance. Every other page returns 403.
* CSRF: every state-changing request must echo the session's CSRF token in the
  ``_csrf`` form field or the ``X-CSRF-Token`` header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from .config import Settings
from .db import Storage, utcnow_iso

logger = logging.getLogger("hrms.auth")

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32

ROLE_HR = "hr"
ROLE_KIOSK = "kiosk"
ROLE_EMPLOYEE = "employee"


# --------------------------------------------------------------------- hashing
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return ``(hash_hex, salt_hex)``."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt), n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN
    )
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    try:
        candidate, _ = hash_password(password, salt)
    except (ValueError, TypeError):  # pragma: no cover - corrupt row
        return False
    return hmac.compare_digest(candidate, password_hash)


def generate_password(length: int = 14) -> str:
    """Readable but strong password (no ambiguous characters)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------- session
@dataclass(slots=True)
class SessionUser:
    id: int
    username: str
    role: str
    display_name: str
    csrf: str
    device_label: str | None = None
    employee_id: int | None = None
    exp: int = 0
    session_version: int = 0

    @property
    def is_hr(self) -> bool:
        return self.role == ROLE_HR

    @property
    def is_kiosk(self) -> bool:
        return self.role == ROLE_KIOSK

    @property
    def is_employee(self) -> bool:
        return self.role == ROLE_EMPLOYEE

    @property
    def initials(self) -> str:
        parts = [p for p in (self.display_name or self.username).split() if p]
        return "".join(p[0].upper() for p in parts[:2]) or "U"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload_b64: str, secret: str) -> str:
    return _b64encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest())


def create_session_token(user: dict[str, Any], settings: Settings, *, lifetime_seconds: int) -> tuple[str, SessionUser]:
    csrf = secrets.token_urlsafe(24)
    payload = {
        "uid": int(user["id"]),
        "u": user["username"],
        "r": user["role"],
        "n": user.get("display_name") or user["username"],
        "d": user.get("device_label"),
        "eid": user.get("employee_id"),
        "csrf": csrf,
        "exp": int(time.time()) + int(lifetime_seconds),
        "sv": int(user.get("session_version") or 0),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    token = f"{encoded}.{_sign(encoded, settings.secret_key)}"
    session = SessionUser(
        id=payload["uid"],
        username=payload["u"],
        role=payload["r"],
        display_name=payload["n"],
        device_label=payload["d"],
        employee_id=payload.get("eid"),
        csrf=csrf,
        exp=payload["exp"],
        session_version=payload["sv"],
    )
    return token, session


def read_session_token(token: str | None, settings: Settings) -> SessionUser | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(encoded, settings.secret_key)):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return SessionUser(
        id=int(payload["uid"]),
        username=payload["u"],
        role=payload["r"],
        display_name=payload.get("n") or payload["u"],
        device_label=payload.get("d"),
        employee_id=payload.get("eid"),
        csrf=payload.get("csrf", ""),
        exp=int(payload["exp"]),
        session_version=int(payload.get("sv", 0)),
    )


def set_session_cookie(response, token: str, settings: Settings, *, max_age: int) -> None:
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


def clear_session_cookie(response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie, path="/")


# ------------------------------------------------------------------ FastAPI deps
def _wants_html(request: Request) -> bool:
    """Decide between an HTML redirect and a JSON 401/403.

    ``/api/*`` is always JSON. For pages we assume HTML unless the caller
    explicitly asked for JSON — that matches browsers (``Accept: text/html``),
    bare curl (``*/*``) and fetch() alike.
    """
    if request.url.path.startswith("/api/"):
        return False
    return "application/json" not in request.headers.get("accept", "")


def get_session(request: Request) -> SessionUser | None:
    """The signed-in user, or ``None``.

    The cookie is signed, but a signature alone cannot be revoked — so every
    request also checks the user row: the login must still exist, be active and
    carry the same ``session_version`` the cookie was issued with. Disabling a
    login, deleting it or changing its password bumps the version and ends every
    session at once (one primary-key lookup per request).
    """
    if hasattr(request.state, "session_checked"):
        return request.state.session_checked
    settings: Settings = request.app.state.settings
    session = read_session_token(request.cookies.get(settings.session_cookie), settings)
    if session is not None:
        storage = getattr(request.app.state, "storage", None)
        row = storage.query_one(
            "SELECT active, session_version FROM users WHERE id = ?", (session.id,)
        ) if storage is not None else None
        if row is None or not row["active"] or int(row["session_version"] or 0) != session.session_version:
            session = None
    request.state.session_checked = session
    return session


def current_user(request: Request) -> SessionUser:
    """Require *any* signed-in user (HR or kiosk)."""
    session = get_session(request)
    if session is None:
        if _wants_html(request):
            target = request.url.path
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": f"/login?next={target}"},
            )
        raise HTTPException(status_code=401, detail="Not signed in")
    request.state.user = session
    return session


def require_employee(request: Request) -> SessionUser:
    """Employee portal pages: the caller must own an employee record."""
    session = current_user(request)
    if not session.is_employee or not session.employee_id:
        if _wants_html(request):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/dashboard" if session.is_hr else "/kiosk"},
            )
        raise HTTPException(status_code=403, detail="Employee portal access required")
    return session


def require_hr_or_kiosk(request: Request) -> SessionUser:
    """The gate scanner. An employee login must not be able to open it."""
    session = current_user(request)
    if session.is_employee:
        # A plain 403 for pages and API alike; browsers get the branded error page.
        raise HTTPException(status_code=403, detail="The gate scanner is for the kiosk device and HR only. "
                                                    "Your attendance is marked when you scan at the gate.")
    return session


def require_hr(request: Request) -> SessionUser:
    session = current_user(request)
    if not session.is_hr:
        if _wants_html(request):
            home = "/me" if session.is_employee else "/kiosk?err=Kiosk+login+can+only+mark+attendance"
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": home})
        raise HTTPException(status_code=403, detail="HR access required")
    return session


def require_csrf(request: Request) -> None:
    """Validate the CSRF token on state-changing requests."""
    session = get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    provided = request.headers.get("x-csrf-token")
    if not provided:
        # Form posts put the token in the body; reading it here is safe because
        # python-multipart caches the parsed form on the request.
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            provided = None  # validated inside the route via form field helper
    if provided and not hmac.compare_digest(provided, session.csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def verify_form_csrf(session: SessionUser, provided: str | None) -> None:
    """Route-level CSRF check for classic HTML form submissions."""
    if not provided or not hmac.compare_digest(provided, session.csrf):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


# ------------------------------------------------------------------ user store
def create_user(
    storage: Storage,
    *,
    username: str,
    password: str,
    role: str,
    display_name: str | None = None,
    device_label: str | None = None,
    employee_id: int | None = None,
) -> dict[str, Any]:
    password_hash, salt = hash_password(password)
    storage.execute(
        """
        INSERT INTO users (username, password_hash, salt, role, display_name, device_label,
                           active, created_at, employee_id)
        VALUES (?,?,?,?,?,?,1,?,?)
        """,
        (username.strip().lower(), password_hash, salt, role, display_name or username,
         device_label, utcnow_iso(), employee_id),
    )
    user = get_user_by_username(storage, username)
    assert user is not None
    return user


def get_user_by_username(storage: Storage, username: str) -> dict[str, Any] | None:
    clean = username.strip().lower()
    user = storage.query_one(
        "SELECT * FROM users WHERE LOWER(username) = ? OR LOWER(COALESCE(email, '')) = ?",
        (clean, clean),
    )
    if user:
        return user
    user = storage.query_one(
        """
        SELECT u.* FROM users u
        JOIN employees e ON e.id = u.employee_id
        WHERE LOWER(COALESCE(e.email, '')) = ?
           OR LOWER(COALESCE(e.code, '')) = ?
           OR LOWER(COALESCE(e.phone, '')) = ?
        ORDER BY u.id LIMIT 1
        """,
        (clean, clean, clean),
    )
    if user:
        return user

    phone_digits = "".join(ch for ch in clean if ch.isdigit())
    if phone_digits and len(phone_digits) >= 7:
        candidates = storage.query(
            """
            SELECT u.*, e.phone AS employee_phone FROM users u
            JOIN employees e ON e.id = u.employee_id
            WHERE e.phone IS NOT NULL AND e.phone != ''
            """
        )
        for cand in candidates:
            raw_phone = str(cand.get("employee_phone") or cand.get("phone") or "")
            cand_digits = "".join(ch for ch in raw_phone if ch.isdigit())
            if not cand_digits:
                continue
            if cand_digits == phone_digits:
                return cand
            if len(phone_digits) >= 10 and cand_digits.endswith(phone_digits[-10:]):
                return cand
            if len(cand_digits) >= 10 and phone_digits.endswith(cand_digits[-10:]):
                return cand

    return None


def get_user(storage: Storage, user_id: int) -> dict[str, Any] | None:
    return storage.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def list_users(storage: Storage) -> list[dict[str, Any]]:
    return storage.query(
        "SELECT id, username, role, display_name, device_label, active, created_at, last_login_at, email "
        "FROM users ORDER BY role, username"
    )


def authenticate(storage: Storage, username: str, password: str) -> dict[str, Any] | None:
    user = get_user_by_username(storage, username)
    if not user or not user["active"]:
        return None
    if not verify_password(password, user["password_hash"], user["salt"]):
        return None
    storage.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow_iso(), user["id"]))
    return user


def revoke_sessions(storage: Storage, user_id: int) -> None:
    """Sign a user out everywhere: every cookie issued so far stops working."""
    storage.execute(
        "UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user_id,)
    )


def change_password(storage: Storage, user_id: int, new_password: str) -> None:
    password_hash, salt = hash_password(new_password)
    storage.execute(
        "UPDATE users SET password_hash = ?, salt = ?, session_version = session_version + 1 WHERE id = ?",
        (password_hash, salt, user_id),
    )


def set_user_active(storage: Storage, user_id: int, active: bool) -> None:
    storage.execute(
        "UPDATE users SET active = ?, session_version = session_version + 1 WHERE id = ?",
        (1 if active else 0, user_id),
    )
    if not active:
        # a reset code issued before the account was disabled must not work afterwards
        storage.execute(
            "UPDATE otp_codes SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (utcnow_iso(), user_id)
        )


def delete_user(storage: Storage, user_id: int) -> None:
    """Delete a login. Records it created stay; they just lose the link (EMP-02)."""
    with storage.transaction():
        storage.execute_tx("UPDATE leave_requests SET applied_by = NULL WHERE applied_by = ?", (user_id,))
        storage.execute_tx(
            "UPDATE profile_change_requests SET requested_by = NULL WHERE requested_by = ?", (user_id,)
        )
        storage.execute_tx("DELETE FROM users WHERE id = ?", (user_id,))


# ---------------------------------------------------------- brute-force guard
#: 5 wrong passwords for one username (or 20 from one IP) in 10 minutes → locked
#: for the rest of that window. Successful sign-in clears the username's slate.
LOCKOUT_WINDOW_SECONDS = 600
LOCKOUT_PER_USER = 5
LOCKOUT_PER_IP = 20


def login_locked(storage: Storage, username: str, ip: str | None) -> bool:
    since = time.time() - LOCKOUT_WINDOW_SECONDS
    per_user = storage.scalar(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE username = ? AND at > ?",
        (username.strip().lower(), since),
    )
    per_ip = storage.scalar(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND at > ?", (ip, since)
    ) if ip else 0
    return int(per_user) >= LOCKOUT_PER_USER or int(per_ip) >= LOCKOUT_PER_IP


def record_login_failure(storage: Storage, username: str, ip: str | None) -> None:
    storage.execute(
        "INSERT INTO login_attempts (username, ip, at) VALUES (?,?,?)",
        (username.strip().lower()[:80], ip, time.time()),
    )
    # keep the table small
    storage.execute("DELETE FROM login_attempts WHERE at < ?", (time.time() - 86400,))


def clear_login_failures(storage: Storage, username: str) -> None:
    storage.execute("DELETE FROM login_attempts WHERE username = ?", (username.strip().lower(),))


CREDENTIALS_FILE = "initial_credentials.txt"


def ensure_default_users(storage: Storage, settings: Settings) -> list[tuple[str, str, str]]:
    """Create the first HR and kiosk logins if the users table is empty."""
    if storage.scalar("SELECT COUNT(*) FROM users", default=0) > 0:
        return []

    created: list[tuple[str, str, str]] = []

    if get_user_by_username(storage, settings.default_hr_username) is None:
        password = settings.default_hr_password or generate_password()
        create_user(
            storage,
            username=settings.default_hr_username,
            password=password,
            role=ROLE_HR,
            display_name="HR Admin",
        )
        storage.audit(actor="system", action="create_user", entity="user", entity_id=settings.default_hr_username,
                      detail="initial HR account")
        created.append((ROLE_HR, settings.default_hr_username, password))

    if get_user_by_username(storage, settings.default_kiosk_username) is None:
        password = settings.default_kiosk_password or generate_password()
        create_user(
            storage,
            username=settings.default_kiosk_username,
            password=password,
            role=ROLE_KIOSK,
            display_name="Gate Scanner",
            device_label="Main Gate",
        )
        storage.audit(actor="system", action="create_user", entity="user", entity_id=settings.default_kiosk_username,
                      detail="initial kiosk account")
        created.append((ROLE_KIOSK, settings.default_kiosk_username, password))

    if created:
        lines = [
            f"{settings.app_name} — initial credentials",
            f"generated: {utcnow_iso()}",
            "",
            "Change these after the first login (Settings → Users).",
            "",
        ]
        for role, username, password in created:
            lines.append(f"role={role:5s}  username={username:12s}  password={password}")
        path = settings.data_dir / CREDENTIALS_FILE
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return created


# ------------------------------------------------------- employee self-service
def get_user_for_employee(storage: Storage, employee_id: int) -> dict[str, Any] | None:
    """The portal login linked to one employee, if it exists."""
    return storage.query_one(
        "SELECT * FROM users WHERE employee_id = ? AND role = ? ORDER BY id LIMIT 1",
        (employee_id, ROLE_EMPLOYEE),
    )


def ensure_employee_login(
    storage: Storage, employee: dict[str, Any], *, password: str | None = None, username: str | None = None
) -> tuple[dict[str, Any], str]:
    """Create (or reset) the portal login for an employee.

    The username is the employee code in lower case, which is unique and stable
    (``FITS001`` → ``fits001``). Returns ``(user, plaintext_password)`` — the
    password is shown once and never stored in readable form.
    """
    code = (username or str(employee.get("code") or "")).strip().lower()
    if not code:
        raise ValueError("Employee has no code, so a login cannot be created.")
    if username and not all(ch.isalnum() or ch in "._-@" for ch in code):
        raise ValueError("A username may use letters, digits and . _ - @ only.")

    existing = get_user_for_employee(storage, employee["id"])
    new_password = password or generate_password()

    if existing:
        change_password(storage, existing["id"], new_password)
        storage.execute(
            "UPDATE users SET display_name = ?, active = 1 WHERE id = ?",
            (employee.get("name") or existing["username"], existing["id"]),
        )
        refreshed = get_user(storage, existing["id"])
        assert refreshed is not None
        return refreshed, new_password

    clash = get_user_by_username(storage, code)
    if clash:
        raise ValueError(
            f"The username '{code}' is already taken by another login — pick another one."
        )

    user = create_user(
        storage,
        username=code,
        password=new_password,
        role=ROLE_EMPLOYEE,
        display_name=employee.get("name") or code,
        employee_id=employee["id"],
    )
    return user, new_password


# ------------------------------------------------------ set-password links (C3)
PASSWORD_LINK_HOURS = 72


def issue_password_link(storage: Storage, user_id: int) -> str:
    """A one-time token for "set your password". Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    storage.execute("UPDATE password_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                    (now, user_id))  # one live link per user
    storage.execute(
        "INSERT INTO password_tokens (user_id, token_hash, created_at, expires_at) VALUES (?,?,?,?)",
        (user_id, hashlib.sha256(token.encode()).hexdigest(), now, now + PASSWORD_LINK_HOURS * 3600),
    )
    return token


def password_link_user(storage: Storage, token: str) -> dict[str, Any] | None:
    """The login a still-valid, unused token belongs to (or ``None``)."""
    row = storage.query_one(
        "SELECT * FROM password_tokens WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (hashlib.sha256((token or "").encode()).hexdigest(), time.time()),
    )
    if row is None:
        return None
    user = get_user(storage, row["user_id"])
    return user if user and user["active"] else None


def use_password_link(storage: Storage, token: str, new_password: str) -> dict[str, Any]:
    user = password_link_user(storage, token)
    if user is None:
        raise ValueError("This link has expired or was already used. Ask HR for a new one.")
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    change_password(storage, user["id"], new_password)
    storage.execute("UPDATE password_tokens SET used_at = ? WHERE token_hash = ?",
                    (time.time(), hashlib.sha256(token.encode()).hexdigest()))
    storage.audit(actor=user["username"], action="password_set_via_link", entity="user", entity_id=user["id"])
    return user


def disable_employee_login(storage: Storage, employee_id: int) -> bool:
    user = get_user_for_employee(storage, employee_id)
    if user is None:
        return False
    set_user_active(storage, user["id"], False)
    return True
